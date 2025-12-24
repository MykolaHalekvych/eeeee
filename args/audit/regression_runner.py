"""
Regression runner for ARGS Core MA.

Loads JSON test cases and compares:
- expected.ma_decision
- expected.enforced_no_trade

Each case provides a ctx snapshot and expected outputs.
Offline only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

from args.ma.policy_loader import load_policy
from args.ma.ma_runtime import eval_ma


@dataclass(frozen=True)
class CaseResult:
    case_name: str
    passed: bool
    expected: Dict[str, Any]
    actual: Dict[str, Any]
    reason: str = ""


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"Case file must be a JSON object: {path}")
    return obj


def _build_ctx(case: Dict[str, Any]) -> Dict[str, Any]:
    ctx = case.get("ctx", {})
    if not isinstance(ctx, dict):
        ctx = {}

    # Ensure standard keys exist (safe defaults).
    return {
        "instrument": ctx.get("instrument", ""),
        "timeframe": ctx.get("timeframe", ""),
        "env": ctx.get("env", ""),
        "data": ctx.get("data", {}) if isinstance(ctx.get("data", {}), dict) else {},
        "state": ctx.get("state", {}) if isinstance(ctx.get("state", {}), dict) else {},
        "risk": ctx.get("risk", {}) if isinstance(ctx.get("risk", {}), dict) else {},
        "exec": ctx.get("exec", {}) if isinstance(ctx.get("exec", {}), dict) else {},
        "pnl": ctx.get("pnl", {}) if isinstance(ctx.get("pnl", {}), dict) else {},
        "stats": ctx.get("stats", {}) if isinstance(ctx.get("stats", {}), dict) else {},
    }


def run_regression(case_paths: List[str], policy_path: str) -> Dict[str, Any]:
    policy = load_policy(policy_path)

    results: List[CaseResult] = []
    total = 0
    passed = 0
    failed = 0

    for path in case_paths:
        total += 1
        case = _load_json(path)
        case_name = str(case.get("case_name", path))

        expected = case.get("expected", {})
        if not isinstance(expected, dict):
            expected = {}

        exp_decision = expected.get("ma_decision")
        exp_enforced = expected.get("enforced_no_trade")

        ctx = _build_ctx(case)
        actual_result = eval_ma(policy, ctx)

        act_decision = actual_result.get("ma_decision")
        act_env = actual_result.get("risk_envelope", {})
        act_enforced = act_env.get("enforced_no_trade") if isinstance(act_env, dict) else None

        ok_decision = (exp_decision == act_decision)
        ok_enforced = (exp_enforced == act_enforced)

        if ok_decision and ok_enforced:
            passed += 1
            results.append(
                CaseResult(
                    case_name=case_name,
                    passed=True,
                    expected={"ma_decision": exp_decision, "enforced_no_trade": exp_enforced},
                    actual={"ma_decision": act_decision, "enforced_no_trade": act_enforced},
                )
            )
        else:
            failed += 1
            reason = []
            if not ok_decision:
                reason.append("ma_decision mismatch")
            if not ok_enforced:
                reason.append("enforced_no_trade mismatch")
            results.append(
                CaseResult(
                    case_name=case_name,
                    passed=False,
                    expected={"ma_decision": exp_decision, "enforced_no_trade": exp_enforced},
                    actual={"ma_decision": act_decision, "enforced_no_trade": act_enforced},
                    reason="; ".join(reason),
                )
            )

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "results": [r.__dict__ for r in results],
    }
