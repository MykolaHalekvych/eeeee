"""
Replay harness (offline) for ARGS Core.

Reads events.jsonl, reconstructs ctx from ctx_snapshot, re-evaluates MA, and compares:
- effective_ma_decision (semantic comparison)
- risk_envelope.enforced_no_trade (strict)

Semantic rule (to avoid legacy drift):
- if enforced_no_trade == True  -> effective decision is NO_TRADE
  (even if legacy logs recorded UNKNOWN)
- otherwise -> decision is normalized (upper, '-' -> '_')

No writes to events.jsonl. Pure offline audit tool.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from args.audit.event_store import iter_events
from args.ma.ma_runtime import eval_ma
from args.ma.policy_loader import load_policy


def _norm_decision(x: Any) -> str:
    return str(x or "UNKNOWN").upper().replace("-", "_")


def _norm_enforced(x: Any) -> Optional[bool]:
    # Keep None as None; only accept real booleans as booleans.
    if x is True:
        return True
    if x is False:
        return False
    return None


def _effective_decision(ma_decision: Any, enforced_no_trade: Optional[bool]) -> str:
    # Canonical semantics for replay:
    # enforced_no_trade=True => effective decision = NO_TRADE
    if enforced_no_trade is True:
        return "NO_TRADE"
    return _norm_decision(ma_decision)


def _build_ctx(event: Dict[str, Any]) -> Dict[str, Any]:
    snap = event.get("ctx_snapshot", {})
    if not isinstance(snap, dict):
        snap = {}

    data = snap.get("data", {})
    state = snap.get("state", {})
    risk = snap.get("risk", {})

    if not isinstance(data, dict):
        data = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(risk, dict):
        risk = {}

    return {
        "instrument": event.get("instrument", ""),
        "timeframe": event.get("timeframe", ""),
        "env": event.get("environment", ""),
        "data": data,
        "state": state,
        "risk": risk,
        "exec": {},
        "pnl": {},
        "stats": {},
    }


def replay_events(
    events_path: str, policy_path: str, max_mismatches: int = 5
) -> Dict[str, Any]:
    policy = load_policy(policy_path)

    total = 0
    matches = 0
    mismatches = 0
    mismatch_examples: List[Dict[str, Any]] = []

    for line_no, event in enumerate(iter_events(events_path), start=1):
        total += 1
        if not isinstance(event, dict):
            continue

        ctx = _build_ctx(event)
        result = eval_ma(policy, ctx)
        if not isinstance(result, dict):
            # defensive: treat as mismatch if result is unexpected type
            mismatches += 1
            if len(mismatch_examples) < max_mismatches:
                mismatch_examples.append(
                    {
                        "line": line_no,
                        "event_id": event.get("event_id"),
                        "expected": {
                            "ma_decision": event.get("ma_decision"),
                            "enforced_no_trade": None,
                        },
                        "actual": {"ma_decision": None, "enforced_no_trade": None},
                    }
                )
            continue

        expected_decision = event.get("ma_decision")
        actual_decision = result.get("ma_decision")

        # enforced_no_trade lives under risk_envelope in both event and result
        expected_enforced = None
        risk_env = event.get("risk_envelope", {})
        if isinstance(risk_env, dict):
            expected_enforced = _norm_enforced(risk_env.get("enforced_no_trade"))

        actual_enforced = None
        actual_env = result.get("risk_envelope", {})
        if isinstance(actual_env, dict):
            actual_enforced = _norm_enforced(actual_env.get("enforced_no_trade"))

        exp_eff = _effective_decision(expected_decision, expected_enforced)
        act_eff = _effective_decision(actual_decision, actual_enforced)

        ok = (exp_eff == act_eff) and (expected_enforced == actual_enforced)

        if ok:
            matches += 1
        else:
            mismatches += 1
            if len(mismatch_examples) < max_mismatches:
                mismatch_examples.append(
                    {
                        "line": line_no,
                        "event_id": event.get("event_id"),
                        "expected": {
                            "ma_decision": expected_decision,
                            "effective_decision": exp_eff,
                            "enforced_no_trade": expected_enforced,
                        },
                        "actual": {
                            "ma_decision": actual_decision,
                            "effective_decision": act_eff,
                            "enforced_no_trade": actual_enforced,
                        },
                    }
                )

    return {
        "total": total,
        "matches": matches,
        "mismatches": mismatches,
        "mismatch_examples": mismatch_examples,
    }
