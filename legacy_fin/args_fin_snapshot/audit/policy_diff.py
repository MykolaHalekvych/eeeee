"""
Policy diff (offline) for ARGS Core.

Compares two MA policies (YAML) and reports:
- decision_set.allowed diff
- risk_envelope.limits diff
- rule changes by rule_id across required blocks

Offline only. No side-effects besides printing via demo runner.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from args.ma.policy_loader import load_policy


def _dict_diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    keys = sorted(set(a.keys()) | set(b.keys()))
    out = {"added": {}, "removed": {}, "changed": {}}

    for k in keys:
        in_a = k in a
        in_b = k in b
        if in_a and not in_b:
            out["removed"][k] = a[k]
        elif in_b and not in_a:
            out["added"][k] = b[k]
        else:
            if a[k] != b[k]:
                out["changed"][k] = {"from": a[k], "to": b[k]}
    return out


def _list_diff(a: List[Any], b: List[Any]) -> Dict[str, Any]:
    sa = set(a)
    sb = set(b)
    return {
        "added": sorted(list(sb - sa)),
        "removed": sorted(list(sa - sb)),
    }


def _rules_by_id(block_rules: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in block_rules:
        rid = str(r.get("rule_id", "")).strip()
        if not rid:
            continue
        out[rid] = r
    return out


def _rule_signature(rule: Dict[str, Any]) -> Tuple[Any, Any, Any, Any]:
    # only compare fields that affect behavior
    return (
        rule.get("enabled", True),
        rule.get("decision"),
        rule.get("enforce", []),
        rule.get("when"),
    )


def diff_policies(old_policy_path: str, new_policy_path: str) -> Dict[str, Any]:
    oldp = load_policy(old_policy_path)
    newp = load_policy(new_policy_path)

    report: Dict[str, Any] = {}

    report["policy_names"] = {"old": oldp.name, "new": newp.name}

    report["decision_set_allowed"] = _list_diff(
        oldp.allowed_decisions, newp.allowed_decisions
    )
    report["risk_limits"] = _dict_diff(oldp.risk_limits, newp.risk_limits)

    # Rule diffs per block
    blocks_report: Dict[str, Any] = {}
    total_rule_changes = 0

    for block_name in newp.blocks.keys():
        old_rules = oldp.blocks.get(block_name, [])
        new_rules = newp.blocks.get(block_name, [])

        old_map = _rules_by_id(old_rules)
        new_map = _rules_by_id(new_rules)

        added = sorted([rid for rid in new_map.keys() if rid not in old_map])
        removed = sorted([rid for rid in old_map.keys() if rid not in new_map])

        changed: List[Dict[str, Any]] = []
        common = sorted([rid for rid in new_map.keys() if rid in old_map])
        for rid in common:
            if _rule_signature(old_map[rid]) != _rule_signature(new_map[rid]):
                changed.append(
                    {
                        "rule_id": rid,
                        "from": {
                            "enabled": old_map[rid].get("enabled", True),
                            "decision": old_map[rid].get("decision"),
                            "enforce": old_map[rid].get("enforce", []),
                            "when": old_map[rid].get("when"),
                        },
                        "to": {
                            "enabled": new_map[rid].get("enabled", True),
                            "decision": new_map[rid].get("decision"),
                            "enforce": new_map[rid].get("enforce", []),
                            "when": new_map[rid].get("when"),
                        },
                    }
                )

        block_changes = len(added) + len(removed) + len(changed)
        total_rule_changes += block_changes

        blocks_report[block_name] = {
            "added": added,
            "removed": removed,
            "changed": changed,
            "counts": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
            },
        }

    report["rules"] = blocks_report
    report["impact_summary"] = {
        "rule_changes_total": total_rule_changes,
        "limits_changes_total": len(report["risk_limits"]["added"])
        + len(report["risk_limits"]["removed"])
        + len(report["risk_limits"]["changed"]),
        "decision_set_changes_total": len(report["decision_set_allowed"]["added"])
        + len(report["decision_set_allowed"]["removed"]),
    }

    return report
