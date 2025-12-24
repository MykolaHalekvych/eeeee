"""
Replay harness (offline) for ARGS Core.

Reads events.jsonl, reconstructs ctx from ctx_snapshot, re-evaluates MA, and compares:
- ma_decision
- risk_envelope.enforced_no_trade

No writes to events.jsonl. Pure offline audit tool.
"""

from typing import Any, Dict, List

from args.audit.event_store import iter_events
from args.ma.policy_loader import load_policy
from args.ma.ma_runtime import eval_ma


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


def replay_events(events_path: str, policy_path: str, max_mismatches: int = 5) -> Dict[str, Any]:
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

        expected_decision = event.get("ma_decision")
        actual_decision = result.get("ma_decision")

        expected_enforced = None
        risk_env = event.get("risk_envelope", {})
        if isinstance(risk_env, dict):
            expected_enforced = risk_env.get("enforced_no_trade")

        actual_env = result.get("risk_envelope", {})
        actual_enforced = None
        if isinstance(actual_env, dict):
            actual_enforced = actual_env.get("enforced_no_trade")

        ok = (expected_decision == actual_decision) and (expected_enforced == actual_enforced)

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
                            "enforced_no_trade": expected_enforced,
                        },
                        "actual": {
                            "ma_decision": actual_decision,
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
