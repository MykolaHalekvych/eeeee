"""
Policy loader for ARGS Core.

Responsibilities:
- Load MA policy from YAML file
- Validate required top-level fields
- Validate rule schema
- Provide read-only access to policy contents
"""

from dataclasses import dataclass
from typing import Any, Dict, List

import yaml


REQUIRED_BLOCKS = [
    "hard_gates",
    "margin_gates",
    "tail_risk_gates",
    "liquidity_gates",
    "correlation_gates",
    "kill_switch.triggers",
]


@dataclass(frozen=True)
class Policy:
    """
    Read-only container for MA policy.
    """

    name: str
    schema_version: str
    instrument: str
    timeframe: str
    environment: str

    allowed_decisions: List[str]
    risk_limits: Dict[str, Any]
    blocks: Dict[str, List[Dict[str, Any]]]


def _require_str(raw: Dict[str, Any], key: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"Missing or invalid required field: {key}")
    return val.strip()


def _require_dict(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    val = raw.get(key)
    if not isinstance(val, dict):
        raise ValueError(f"Missing or invalid required mapping: {key}")
    return val


def _validate_blocks(blocks: Dict[str, Any], allowed_decisions: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    # blocks must contain all required names
    for b in REQUIRED_BLOCKS:
        if b not in blocks:
            raise ValueError(f"Missing required block: {b}")

    normalized: Dict[str, List[Dict[str, Any]]] = {}
    seen_rule_ids: set[str] = set()

    for block_name in REQUIRED_BLOCKS:
        block_obj = blocks.get(block_name)
        if not isinstance(block_obj, dict):
            raise ValueError(f"blocks.{block_name} must be a mapping")

        rules = block_obj.get("rules")
        if not isinstance(rules, list):
            raise ValueError(f"blocks.{block_name}.rules must be a list")

        out_rules: List[Dict[str, Any]] = []
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                raise ValueError(f"blocks.{block_name}.rules[{i}] must be a mapping")

            rule_id = r.get("rule_id")
            when = r.get("when")
            decision = r.get("decision")
            enforce = r.get("enforce")
            reason = r.get("reason")
            enabled = r.get("enabled", True)

            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError(f"blocks.{block_name}.rules[{i}].rule_id must be a non-empty string")
            rid = rule_id.strip()
            if rid in seen_rule_ids:
                raise ValueError(f"Duplicate rule_id: {rid}")
            seen_rule_ids.add(rid)

            if not isinstance(when, dict):
                raise ValueError(f"blocks.{block_name}.rules[{i}].when must be an expr object (mapping)")

            if not isinstance(decision, str) or decision not in allowed_decisions:
                raise ValueError(f"blocks.{block_name}.rules[{i}].decision must be one of decision_set.allowed")

            if enforce is None:
                enforce = []
            if not isinstance(enforce, list) or not all(isinstance(x, str) for x in enforce):
                raise ValueError(f"blocks.{block_name}.rules[{i}].enforce must be a list of strings (can be empty)")

            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"blocks.{block_name}.rules[{i}].reason must be a non-empty string")

            if not isinstance(enabled, bool):
                raise ValueError(f"blocks.{block_name}.rules[{i}].enabled must be bool if provided")

            out_rules.append(
                {
                    "rule_id": rid,
                    "when": when,
                    "decision": decision,
                    "enforce": [x.strip() for x in enforce if isinstance(x, str)],
                    "reason": reason.strip(),
                    "enabled": enabled,
                }
            )

        normalized[block_name] = out_rules

    return normalized


def load_policy(path: str) -> Policy:
    """
    Load MA policy from YAML file and perform validation.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError("Policy YAML must contain a top-level mapping")

    decision_set = _require_dict(raw, "decision_set")
    risk_envelope = _require_dict(raw, "risk_envelope")
    blocks = _require_dict(raw, "blocks")

    allowed = decision_set.get("allowed")
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x.strip() for x in allowed):
        raise ValueError("decision_set.allowed must be a non-empty list of strings")
    allowed_clean = [x.strip() for x in allowed]

    limits = risk_envelope.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("risk_envelope.limits must be a mapping")

    normalized_blocks = _validate_blocks(blocks, allowed_clean)

    return Policy(
        name=_require_str(raw, "policy_name"),
        schema_version=_require_str(raw, "schema_version"),
        instrument=_require_str(raw, "instrument"),
        timeframe=_require_str(raw, "timeframe"),
        environment=_require_str(raw, "environment"),
        allowed_decisions=allowed_clean,
        risk_limits=limits,
        blocks=normalized_blocks,
    )
