"""
Mini rule-engine for ARGS Core.

Supported expr:
- {"any": [expr, ...]}
- {"all": [expr, ...]}
- atom: {"path": "risk.margin_usage", "op": "gt", "value": 0.35}

Ops:
eq, neq, gt, gte, lt, lte, in, contains

Safety:
- Missing path => atom evaluates to False
- Any type mismatch / unexpected shape => evaluates to False (no exceptions)
"""

from dataclasses import dataclass
from typing import Any, Mapping, Tuple


@dataclass(frozen=True)
class EvalResult:
    matched: bool
    detail: str = ""


def _get_by_path(ctx: Mapping[str, Any], path: str) -> Tuple[bool, Any]:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return False, None
        if part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _safe_compare(op: str, actual: Any, expected: Any) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "neq":
            return actual != expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "in":
            # actual is element, expected is container
            if isinstance(expected, (list, tuple, set, frozenset)):
                return actual in expected
            return False
        if op == "contains":
            # actual is container, expected is element
            if isinstance(actual, (list, tuple, set, frozenset, str)):
                return expected in actual
            if isinstance(actual, Mapping):
                return expected in actual
            return False
    except Exception:
        return False
    return False


def eval_expr(expr: Any, ctx: Mapping[str, Any]) -> EvalResult:
    if not isinstance(expr, Mapping):
        return EvalResult(False, "expr_not_mapping")

    if "any" in expr:
        items = expr.get("any")
        if not isinstance(items, list):
            return EvalResult(False, "any_not_list")
        for sub in items:
            if eval_expr(sub, ctx).matched:
                return EvalResult(True, "")
        return EvalResult(False, "")

    if "all" in expr:
        items = expr.get("all")
        if not isinstance(items, list) or len(items) == 0:
            return EvalResult(False, "all_not_list_or_empty")
        for sub in items:
            if not eval_expr(sub, ctx).matched:
                return EvalResult(False, "")
        return EvalResult(True, "")

    # atom
    path = expr.get("path")
    op = expr.get("op")
    expected = expr.get("value")

    if not isinstance(path, str) or not isinstance(op, str):
        return EvalResult(False, "atom_missing_fields")

    ok, actual = _get_by_path(ctx, path)
    if not ok:
        return EvalResult(False, "path_missing")

    return EvalResult(_safe_compare(op, actual, expected), "")
