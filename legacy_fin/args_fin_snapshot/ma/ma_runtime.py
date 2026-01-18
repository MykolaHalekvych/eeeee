"""
MA runtime (Management / Risk Gate) for ARGS Core.

Key properties:
- Deterministic: same input => same output
- No side-effects
- Evaluates ALL rules (no short-circuit), collects violations, then decides
- UNKNOWN is valid and safety-first
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

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

_VALID_GLOBAL_MODES = {"NO_TRADE", "ONLY_EXITS", "ALLOW_NEW_ENTRIES"}


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
    """
    Built-in hard gate: ensure minimal required fields exist.
    """
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
            decision="NO_TRADE",
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


def _normalize_global_mode(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip().upper()
    return s if s in _VALID_GLOBAL_MODES else None


def _extract_pos_from_position_state(position_state: Any) -> float:
    """
    Best-effort extraction of net position.

    Supports common shapes used across ARGS:
      - position_state.size (int)
      - position_state.pos / net_pos / qty / net_qty
    """
    if position_state is None:
        return 0.0

    if isinstance(position_state, Mapping):
        for k in (
            "pos",
            "net_pos",
            "size",
            "position",
            "position_qty",
            "qty",
            "net_qty",
        ):
            if k in position_state:
                try:
                    return float(position_state.get(k) or 0.0)
                except Exception:
                    return 0.0
        return 0.0

    for attr in (
        "pos",
        "net_pos",
        "size",
        "position",
        "position_qty",
        "qty",
        "net_qty",
    ):
        if hasattr(position_state, attr):
            try:
                return float(getattr(position_state, attr) or 0.0)
            except Exception:
                return 0.0

    return 0.0


def _get_exec_global_mode(ctx: Mapping[str, Any]) -> Optional[str]:
    """
    Stage 5.8+: authoritative operator intent is ctx.exec.global_mode.
    """
    try:
        ex = ctx.get("exec", {})
        if isinstance(ex, Mapping):
            return _normalize_global_mode(ex.get("global_mode"))
    except Exception:
        pass
    return None


def compute_risk_envelope_mode(
    *,
    enforced_no_trade: bool,
    position_state: Any,
    exec_global_mode: Optional[str],
) -> Tuple[str, str]:
    """
    Spec:
      1) enforced_no_trade=True => mode NO_TRADE always
      2) else base mode depends on position_state:
            pos!=0 => ONLY_EXITS
            pos==0 => NO_TRADE
      3) then override from ctx.exec.global_mode (NO_TRADE/ONLY_EXITS/ALLOW_NEW_ENTRIES)
    Returns: (final_mode, mode_source)
      mode_source: ENFORCED | BASE_POSITION | EXEC_OVERRIDE
    """
    if enforced_no_trade:
        return "NO_TRADE", "ENFORCED"

    pos = _extract_pos_from_position_state(position_state)
    base_mode = "ONLY_EXITS" if abs(pos) > 1e-12 else "NO_TRADE"

    if exec_global_mode in _VALID_GLOBAL_MODES:
        return exec_global_mode, "EXEC_OVERRIDE"

    return base_mode, "BASE_POSITION"


def eval_ma(policy: Policy, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate MA policy on ctx and return:
    {
      "ma_decision": str,           # effective decision
      "ma_decision_raw": str,       # raw strictest vote
      "violations": list[dict],
      "risk_envelope": dict
    }
    """
    violations: List[Violation] = []
    decision_votes: List[str] = []
    enforce_flags: List[str] = []

    # 0) Required fields gate (built-in)
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

            enforce_clean = [
                str(x).strip() for x in enforce if isinstance(x, str) and x.strip()
            ]

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

    # 2) Raw decision (strictest vote)
    if decision_votes:
        ma_decision_raw = _strictest(decision_votes, default="ALLOW")
    else:
        ma_decision_raw = "UNKNOWN"

    # 3) Enforcement + Risk envelope (single computation; no overwrites)
    enforced_no_trade = ("NO_TRADE" in enforce_flags) or (
        ma_decision_raw in {"UNKNOWN", "NO_TRADE", "EXIT"}
    )

    ps = ctx.get("position_state", {})
    pos_val = _extract_pos_from_position_state(ps)
    has_position = abs(pos_val) > 1e-12

    exec_global_mode = _get_exec_global_mode(ctx)

    mode, mode_source = compute_risk_envelope_mode(
        enforced_no_trade=enforced_no_trade,
        position_state=ps,
        exec_global_mode=exec_global_mode,
    )

    # Minimal observability invariant (does not change decisions)
    mode_invariant_ok = True
    if (
        (not enforced_no_trade)
        and (exec_global_mode in _VALID_GLOBAL_MODES)
        and (mode != exec_global_mode)
    ):
        mode_invariant_ok = False

    risk_envelope = {
        "limits": dict(policy.risk_limits),
        "enforced_no_trade": enforced_no_trade,
        "mode": mode,
        "mode_source": mode_source,
        "exec_global_mode": exec_global_mode,
        "mode_invariant_ok": mode_invariant_ok,
        "has_position": has_position,
        "position_size": int(pos_val) if float(pos_val).is_integer() else pos_val,
    }

    # 4) Effective decision
    # If raw decision is UNKNOWN but NO_TRADE is explicitly enforced -> report NO_TRADE as final decision.
    # Do NOT override EXIT.
    ma_decision = ma_decision_raw
    if ma_decision_raw == "UNKNOWN" and ("NO_TRADE" in enforce_flags):
        ma_decision = "NO_TRADE"

    return {
        "ma_decision": ma_decision,
        "ma_decision_raw": ma_decision_raw,
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
