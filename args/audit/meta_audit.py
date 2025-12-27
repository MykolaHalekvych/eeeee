from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from args.ma.policy_loader import load_policy


# -----------------------------
# Severity
# -----------------------------
SEV_FAIL = "FAIL"
SEV_WARN = "WARNING"
SEV_INFO = "INFO"

# -----------------------------
# Strictness (higher = stricter)
# Treat UNKNOWN as conservative (≈ NO_TRADE)
# -----------------------------
STRICTNESS: Dict[str, int] = {
    "BAN": 6,
    "EXIT": 5,
    "NO_TRADE": 4,
    "UNKNOWN": 4,
    "REDUCE": 3,
    "ALLOW": 1,
}

# -----------------------------
# Safety-critical blocks (pre-Level-5)
# Any softening or condition change here is treated as FAIL.
# -----------------------------
SAFETY_BLOCKS: List[str] = [
    "hard_gates",
    "kill_switch.triggers",
    "margin_gates",
    "liquidity_gates",
    "tail_risk_gates",
    "correlation_gates",
]

# Supported DSL ops in rule_engine (must match runtime)
ALLOWED_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "contains"}


# -----------------------------
# Data structures
# -----------------------------
@dataclass(frozen=True)
class Issue:
    severity: str
    key: str
    message: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "key": self.key,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    enabled: bool
    decision: Optional[str]
    enforce: List[str]
    when: Any
    index: int


# -----------------------------
# Helpers
# -----------------------------
def _issue(sev: str, key: str, msg: str, **details: Any) -> Issue:
    return Issue(severity=sev, key=key, message=msg, details=details)


def _norm_decision(x: Any) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, str):
        s = x.strip().upper().replace("-", "_")
        return s if s else None
    return None


def _strictness(decision: Optional[str]) -> int:
    if not decision:
        return 0
    return STRICTNESS.get(decision, 0)


def _norm_enforce(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, list):
        out: List[str] = []
        for v in x:
            if v is None:
                continue
            s = str(v).strip().upper().replace("-", "_")
            if s:
                out.append(s)
        return out
    return []


def _norm_enabled(x: Any, default: bool = True) -> Tuple[Optional[bool], Optional[str]]:
    """
    Strict bool parsing:
    - None -> default
    - bool -> value
    - otherwise -> invalid
    """
    if x is None:
        return default, None
    if isinstance(x, bool):
        return x, None
    return None, f"enabled_not_bool:{type(x).__name__}"


def _to_float(x: Any) -> Optional[float]:
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def _load_raw_yaml(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return None, "YAML parsed to None (empty file?)"
        if not isinstance(raw, dict):
            return None, f"YAML top-level is not a mapping/dict (type={type(raw).__name__})"
        return raw, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _validate_with_loader(path: str) -> Optional[str]:
    try:
        load_policy(path)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _get_blocks(raw_policy: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[Issue]]:
    issues: List[Issue] = []
    blocks = raw_policy.get("blocks")
    if blocks is None:
        issues.append(_issue(SEV_FAIL, "blocks", "Missing required top-level key: blocks"))
        return None, issues
    if not isinstance(blocks, dict):
        issues.append(_issue(SEV_FAIL, "blocks", "blocks is not a dict/mapping", value_type=type(blocks).__name__))
        return None, issues
    return blocks, issues


# -----------------------------
# DSL validation (prevents silent rule disable)
# -----------------------------
def _validate_when_expr(expr: Any) -> List[str]:
    """
    Strict DSL validation (must match runtime rule_engine shapes):
      - {"any": [expr, ...]}  non-empty list
      - {"all": [expr, ...]}  non-empty list
      - atom: {"path": "...", "op": "...", "value": ...} with op in ALLOWED_OPS
    """
    errs: List[str] = []
    if not isinstance(expr, dict):
        return ["when_not_dict"]

    if "any" in expr:
        items = expr.get("any")
        if not isinstance(items, list) or len(items) == 0:
            return ["any_not_list_or_empty"]
        for j, sub in enumerate(items):
            sub_errs = _validate_when_expr(sub)
            errs.extend([f"any[{j}].{e}" for e in sub_errs])
        return errs

    if "all" in expr:
        items = expr.get("all")
        if not isinstance(items, list) or len(items) == 0:
            return ["all_not_list_or_empty"]
        for j, sub in enumerate(items):
            sub_errs = _validate_when_expr(sub)
            errs.extend([f"all[{j}].{e}" for e in sub_errs])
        return errs

    # atom
    path = expr.get("path")
    op = expr.get("op")

    if not isinstance(path, str) or not path.strip():
        errs.append("atom_path_missing_or_empty")
    if not isinstance(op, str) or not op.strip():
        errs.append("atom_op_missing_or_empty")
    else:
        op_u = op.strip().lower()
        if op_u not in ALLOWED_OPS:
            errs.append(f"atom_op_invalid:{op_u}")

    if "value" not in expr:
        errs.append("atom_value_missing")

    # refuse unknown top keys to avoid accidental shapes
    # (allow only atom keys: path/op/value)
    for k in expr.keys():
        if k not in ("path", "op", "value"):
            errs.append(f"atom_unknown_key:{k}")
            break

    return errs


# -----------------------------
# Extractors
# -----------------------------
def _extract_allowed(raw_policy: Dict[str, Any]) -> Tuple[Optional[List[str]], List[Issue]]:
    issues: List[Issue] = []
    ds = raw_policy.get("decision_set")
    if ds is None:
        issues.append(_issue(SEV_WARN, "decision_set", "decision_set missing; governance weaker."))
        return None, issues
    if not isinstance(ds, dict):
        issues.append(_issue(SEV_FAIL, "decision_set", "decision_set not a dict.", value_type=type(ds).__name__))
        return None, issues

    allowed = ds.get("allowed")
    if allowed is None:
        issues.append(_issue(SEV_WARN, "decision_set.allowed", "allowed missing; governance weaker."))
        return None, issues
    if not isinstance(allowed, list):
        issues.append(_issue(SEV_FAIL, "decision_set.allowed", "allowed not a list.", value_type=type(allowed).__name__))
        return None, issues

    out: List[str] = []
    for v in allowed:
        d = _norm_decision(v)
        if d:
            out.append(d)
    if not out:
        issues.append(_issue(SEV_FAIL, "decision_set.allowed", "allowed list empty after normalization."))
        return None, issues

    return out, issues


def _extract_limits(raw_policy: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Issue]]:
    issues: List[Issue] = []
    re_obj = raw_policy.get("risk_envelope")
    if re_obj is None:
        issues.append(_issue(SEV_WARN, "risk_envelope", "risk_envelope missing; skipping limits checks."))
        return {}, issues
    if not isinstance(re_obj, dict):
        issues.append(_issue(SEV_FAIL, "risk_envelope", "risk_envelope not a dict.", value_type=type(re_obj).__name__))
        return {}, issues

    limits = re_obj.get("limits")
    if limits is None:
        issues.append(_issue(SEV_WARN, "risk_envelope.limits", "limits missing; skipping."))
        return {}, issues
    if not isinstance(limits, dict):
        issues.append(_issue(SEV_FAIL, "risk_envelope.limits", "limits not a dict.", value_type=type(limits).__name__))
        return {}, issues

    return limits, issues


def _extract_block_rules(
    blocks: Dict[str, Any],
    block_name: str,
    allowed_decisions: Optional[List[str]],
) -> Tuple[Dict[str, RuleSpec], List[Issue]]:
    """
    Expects blocks.<block_name> to be a dict with rules: [ ... ].
    Returns {rule_id: RuleSpec} + issues.
    Strict for safety blocks.
    """
    issues: List[Issue] = []
    out: Dict[str, RuleSpec] = {}

    block = blocks.get(block_name)
    if block is None:
        # For safety blocks, missing is FAIL
        if block_name in SAFETY_BLOCKS:
            issues.append(_issue(SEV_FAIL, f"blocks.{block_name}", "Missing safety block.", block=block_name))
        return out, issues

    if not isinstance(block, dict):
        sev = SEV_FAIL if block_name in SAFETY_BLOCKS else SEV_WARN
        issues.append(_issue(sev, f"blocks.{block_name}", "Block is not a dict.", value_type=type(block).__name__))
        return out, issues

    rules = block.get("rules")
    if rules is None:
        sev = SEV_FAIL if block_name in SAFETY_BLOCKS else SEV_WARN
        issues.append(_issue(sev, f"blocks.{block_name}.rules", "rules missing.", block=block_name))
        return out, issues
    if not isinstance(rules, list):
        sev = SEV_FAIL if block_name in SAFETY_BLOCKS else SEV_WARN
        issues.append(_issue(sev, f"blocks.{block_name}.rules", "rules is not a list.", value_type=type(rules).__name__))
        return out, issues

    for idx, item in enumerate(rules):
        if not isinstance(item, dict):
            sev = SEV_FAIL if block_name in SAFETY_BLOCKS else SEV_WARN
            issues.append(_issue(sev, f"blocks.{block_name}.rules[{idx}]", "Rule item is not a dict.", item_type=type(item).__name__))
            continue

        rid = item.get("rule_id")
        if not isinstance(rid, str) or not rid.strip():
            issues.append(_issue(SEV_FAIL, f"blocks.{block_name}.rules[{idx}].rule_id", "rule_id missing/empty.", block=block_name, index=idx))
            continue
        rid = rid.strip()

        if rid in out:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", "Duplicate rule_id in block.", block=block_name, rule_id=rid))
            continue

        enabled_raw = item.get("enabled", True)
        enabled, e_err = _norm_enabled(enabled_raw, default=True)
        if enabled is None:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.enabled", "enabled must be bool.", block=block_name, rule_id=rid, value=enabled_raw, error=e_err))
            # Fail closed: treat invalid as disabled softening
            enabled = False

        decision = _norm_decision(item.get("decision"))
        if decision is None:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.decision", "decision missing.", block=block_name, rule_id=rid))

        enforce = _norm_enforce(item.get("enforce"))
        # If allowed decisions exist, enforce values must belong to allowed set
        if allowed_decisions is not None:
            bad = [x for x in enforce if x not in allowed_decisions]
            if bad:
                issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.enforce", "enforce contains values not in decision_set.allowed.", block=block_name, rule_id=rid, bad=bad))

        when = item.get("when")
        when_errs = _validate_when_expr(when)
        if when_errs:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.when", "Invalid when DSL (prevents silent disable).", block=block_name, rule_id=rid, errors=when_errs))

        # decision must be in allowed decisions if known
        if allowed_decisions is not None and decision is not None and decision not in allowed_decisions:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.decision", "decision not in decision_set.allowed.", block=block_name, rule_id=rid, decision=decision))

        out[rid] = RuleSpec(
            rule_id=rid,
            enabled=bool(enabled),
            decision=decision,
            enforce=enforce,
            when=when,
            index=idx,
        )

    return out, issues


# -----------------------------
# Comparators
# -----------------------------
def _compare_allowed(b_allowed: Optional[List[str]], c_allowed: Optional[List[str]]) -> List[Issue]:
    issues: List[Issue] = []
    if b_allowed is None and c_allowed is None:
        return issues
    if b_allowed is not None and c_allowed is None:
        issues.append(_issue(SEV_WARN, "decision_set.allowed", "Candidate missing allowed set while baseline has it."))
        return issues
    if b_allowed is None and c_allowed is not None:
        issues.append(_issue(SEV_WARN, "decision_set.allowed", "Baseline missing allowed set while candidate has it."))
        return issues

    assert b_allowed is not None and c_allowed is not None
    b_set = set(b_allowed)
    c_set = set(c_allowed)

    if b_set != c_set:
        added = sorted(list(c_set - b_set))
        removed = sorted(list(b_set - c_set))

        sev = SEV_WARN
        for core in ("NO_TRADE", "UNKNOWN"):
            if core in b_set and core not in c_set:
                sev = SEV_FAIL

        issues.append(
            _issue(
                sev,
                "decision_set.allowed",
                "Allowed decision set changed.",
                added=added,
                removed=removed,
                baseline=sorted(list(b_set)),
                candidate=sorted(list(c_set)),
            )
        )

    return issues


def _compare_limits(b_limits: Dict[str, Any], c_limits: Dict[str, Any]) -> List[Issue]:
    issues: List[Issue] = []

    # Guardrails (conservative defaults)
    CONF_MIN_FLOOR_FAIL = 0.55
    MARGIN_MAX_CEILING_FAIL = 0.65
    MARGIN_MAX_FLOOR_WARN = 0.10

    def get_num(d: Dict[str, Any], key: str) -> Optional[float]:
        return _to_float(d.get(key)) if key in d else None

    keys = sorted(set(b_limits.keys()) | set(c_limits.keys()))
    for k in keys:
        if k not in ("conf_min", "margin_max", "timestamp_drift_ms_max", "missing_bars_max"):
            continue

        b = get_num(b_limits, k)
        c = get_num(c_limits, k)

        if b is None and c is None:
            continue
        if b is not None and c is None:
            sev = SEV_WARN if k != "conf_min" else SEV_FAIL
            issues.append(_issue(sev, f"limits.{k}", "Candidate removed limit present in baseline.", baseline=b, candidate=None))
            continue
        if b is None and c is not None:
            issues.append(_issue(SEV_INFO, f"limits.{k}", "Candidate added new limit.", baseline=None, candidate=c))
            continue

        assert b is not None and c is not None
        if c == b:
            continue

        if k == "conf_min":
            if c < b:
                issues.append(_issue(SEV_FAIL, "limits.conf_min", "conf_min decreased (more permissive).", baseline=b, candidate=c))
            if c < CONF_MIN_FLOOR_FAIL:
                issues.append(_issue(SEV_FAIL, "limits.conf_min", "conf_min below safety floor.", baseline=b, candidate=c, floor=CONF_MIN_FLOOR_FAIL))
            if c > b:
                issues.append(_issue(SEV_INFO, "limits.conf_min", "conf_min increased (more conservative).", baseline=b, candidate=c))

        elif k == "margin_max":
            if c > b:
                issues.append(_issue(SEV_WARN, "limits.margin_max", "margin_max increased (more permissive).", baseline=b, candidate=c))
            else:
                issues.append(_issue(SEV_INFO, "limits.margin_max", "margin_max decreased (more conservative).", baseline=b, candidate=c))

            if c > MARGIN_MAX_CEILING_FAIL:
                issues.append(_issue(SEV_FAIL, "limits.margin_max", "margin_max above safety ceiling.", baseline=b, candidate=c, ceiling=MARGIN_MAX_CEILING_FAIL))
            if c < MARGIN_MAX_FLOOR_WARN:
                issues.append(_issue(SEV_WARN, "limits.margin_max", "margin_max very low (may cause always-NO_TRADE).", baseline=b, candidate=c, floor_warn=MARGIN_MAX_FLOOR_WARN))

        else:
            issues.append(_issue(SEV_INFO, f"limits.{k}", "Limit changed.", baseline=b, candidate=c))

    return issues


def _compare_rules(
    baseline: Dict[str, RuleSpec],
    candidate: Dict[str, RuleSpec],
    block_name: str,
    enforce_must_include: Optional[str] = None,
) -> List[Issue]:
    issues: List[Issue] = []

    # Baseline -> candidate checks
    for rid, br in baseline.items():
        if rid not in candidate:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", "Rule removed in candidate.", block=block_name, rule_id=rid))
            continue

        cr = candidate[rid]

        # enabled flip (baseline enabled -> candidate disabled is softening)
        if br.enabled and (not cr.enabled):
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", "Rule disabled in candidate (softening).", block=block_name, rule_id=rid))

        # decision softening
        if br.decision and not cr.decision:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", "Decision missing in candidate while baseline had it.", block=block_name, rule_id=rid, baseline_decision=br.decision))
        elif br.decision and cr.decision:
            b_s = _strictness(br.decision)
            c_s = _strictness(cr.decision)
            if c_s < b_s:
                issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", "Decision softened (more permissive).", block=block_name, rule_id=rid, baseline_decision=br.decision, candidate_decision=cr.decision))
            elif c_s > b_s:
                issues.append(_issue(SEV_INFO, f"{block_name}.{rid}", "Decision became stricter.", block=block_name, rule_id=rid, baseline_decision=br.decision, candidate_decision=cr.decision))
            elif cr.decision != br.decision:
                issues.append(_issue(SEV_WARN, f"{block_name}.{rid}", "Decision changed but strictness equal.", block=block_name, rule_id=rid, baseline_decision=br.decision, candidate_decision=cr.decision))

        # enforce weakening
        if enforce_must_include:
            must = enforce_must_include.upper().replace("-", "_")
            if must in br.enforce and must not in cr.enforce:
                issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}", f"Enforce weakened: '{must}' removed.", block=block_name, rule_id=rid, baseline_enforce=br.enforce, candidate_enforce=cr.enforce))

        # WHEN change (critical): any change to conditions is treated as FAIL
        if br.when != cr.when:
            issues.append(_issue(SEV_FAIL, f"{block_name}.{rid}.when", "Rule 'when' changed (treated as softening risk).", block=block_name, rule_id=rid))

    # New rules in candidate (INFO)
    for rid, cr in candidate.items():
        if rid not in baseline:
            issues.append(_issue(SEV_INFO, f"{block_name}.{rid}", "New rule added in candidate.", block=block_name, rule_id=rid, candidate_decision=cr.decision))

    return issues


# -----------------------------
# Main entry
# -----------------------------
def run_meta_audit(baseline_path: str, candidate_path: str) -> Dict[str, Any]:
    issues: List[Issue] = []

    # Read RAW YAML (source of truth for governance comparisons)
    b_raw, b_err = _load_raw_yaml(baseline_path)
    c_raw, c_err = _load_raw_yaml(candidate_path)

    if b_err:
        issues.append(_issue(SEV_FAIL, "baseline.yaml", "Failed to parse baseline YAML.", path=baseline_path, error=b_err))
    if c_err:
        issues.append(_issue(SEV_FAIL, "candidate.yaml", "Failed to parse candidate YAML.", path=candidate_path, error=c_err))

    # Validate with loader (schema/required blocks) — still offline, just validation
    b_val = _validate_with_loader(baseline_path)
    c_val = _validate_with_loader(candidate_path)
    if b_val:
        issues.append(_issue(SEV_FAIL, "baseline.validation", "Baseline policy failed loader validation.", path=baseline_path, error=b_val))
    if c_val:
        issues.append(_issue(SEV_FAIL, "candidate.validation", "Candidate policy failed loader validation.", path=candidate_path, error=c_val))

    # If we cannot parse raw YAML, return early
    if b_raw is None or c_raw is None:
        fail = sum(1 for it in issues if it.severity == SEV_FAIL)
        warn = sum(1 for it in issues if it.severity == SEV_WARN)
        info = sum(1 for it in issues if it.severity == SEV_INFO)
        return {"summary": {"FAIL": fail, "WARNING": warn, "INFO": info}, "issues": [it.to_dict() for it in issues]}

    # decision_set.allowed (extract early; used for enforce/decision checks)
    b_allowed, b_allowed_issues = _extract_allowed(b_raw)
    c_allowed, c_allowed_issues = _extract_allowed(c_raw)
    issues.extend(b_allowed_issues)
    issues.extend(c_allowed_issues)
    issues.extend(_compare_allowed(b_allowed, c_allowed))

    # blocks
    b_blocks, b_block_issues = _get_blocks(b_raw)
    c_blocks, c_block_issues = _get_blocks(c_raw)
    issues.extend(b_block_issues)
    issues.extend(c_block_issues)

    if b_blocks is None or c_blocks is None:
        fail = sum(1 for it in issues if it.severity == SEV_FAIL)
        warn = sum(1 for it in issues if it.severity == SEV_WARN)
        info = sum(1 for it in issues if it.severity == SEV_INFO)
        return {"summary": {"FAIL": fail, "WARNING": warn, "INFO": info}, "issues": [it.to_dict() for it in issues]}

    # Compare safety-critical blocks
    for block_name in SAFETY_BLOCKS:
        b_map, b_issues = _extract_block_rules(b_blocks, block_name, allowed_decisions=b_allowed)
        c_map, c_issues = _extract_block_rules(c_blocks, block_name, allowed_decisions=c_allowed)

        issues.extend(b_issues)
        issues.extend(c_issues)

        # If baseline has block and candidate doesn't -> FAIL (handled in extractor too)
        if b_map and not c_map:
            issues.append(_issue(SEV_FAIL, f"blocks.{block_name}", "Candidate missing block while baseline has it.", block=block_name))

        must_enforce = "NO_TRADE" if block_name in {"hard_gates", "kill_switch.triggers"} else None
        issues.extend(_compare_rules(b_map, c_map, block_name, enforce_must_include=must_enforce))

    # limits
    b_limits, b_lim_issues = _extract_limits(b_raw)
    c_limits, c_lim_issues = _extract_limits(c_raw)
    issues.extend(b_lim_issues)
    issues.extend(c_lim_issues)
    issues.extend(_compare_limits(b_limits, c_limits))

    # Summary
    fail = sum(1 for it in issues if it.severity == SEV_FAIL)
    warn = sum(1 for it in issues if it.severity == SEV_WARN)
    info = sum(1 for it in issues if it.severity == SEV_INFO)

    return {"summary": {"FAIL": fail, "WARNING": warn, "INFO": info}, "issues": [it.to_dict() for it in issues]}


def _print_human(report: Dict[str, Any]) -> None:
    s = report.get("summary", {})
    print(f"META_AUDIT FAIL={s.get('FAIL', 0)} WARNING={s.get('WARNING', 0)} INFO={s.get('INFO', 0)}")
    for it in report.get("issues", []):
        sev = it.get("severity", "?")
        key = it.get("key", "?")
        msg = it.get("message", "")
        print(f"[{sev}] {key}: {msg}")
        details = it.get("details", {})
        if details:
            print("  details:", json.dumps(details, ensure_ascii=True, sort_keys=True))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meta_audit",
        description="Offline governance checks for MA policy changes (hardened: safety blocks + when/op DSL).",
    )
    parser.add_argument("baseline", help="Path to baseline policy YAML (trusted).")
    parser.add_argument("candidate", help="Path to candidate policy YAML (changed).")
    parser.add_argument("--json", action="store_true", help="Print full JSON report after human output.")

    args = parser.parse_args(argv)

    report = run_meta_audit(args.baseline, args.candidate)
    _print_human(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))

    return 2 if report["summary"]["FAIL"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
