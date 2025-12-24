from __future__ import annotations

from pathlib import Path
import sys

import yaml

from args.audit.meta_audit import run_meta_audit


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        obj = yaml.safe_load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"YAML top-level must be dict: {path}")
    return obj


def _save_yaml(path: Path, obj: dict) -> None:
    # Preserve readable YAML; ordering is fine as-is for this repo.
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)


def _reset_copy(baseline: Path, copy_path: Path) -> None:
    copy_path.write_text(baseline.read_text(encoding="utf-8"), encoding="utf-8")


def _flip_data_integrity_gate_to_allow(copy_path: Path) -> None:
    obj = _load_yaml(copy_path)
    blocks = obj.get("blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Missing or invalid blocks in copy policy")

    hg = blocks.get("hard_gates")
    if not isinstance(hg, dict):
        raise ValueError("Missing or invalid blocks.hard_gates in copy policy")

    rules = hg.get("rules")
    if not isinstance(rules, list):
        raise ValueError("Missing or invalid blocks.hard_gates.rules in copy policy")

    hit = False
    for r in rules:
        if isinstance(r, dict) and r.get("rule_id") == "data_integrity_gate":
            r["decision"] = "ALLOW"
            hit = True
            break

    if not hit:
        raise ValueError("Rule data_integrity_gate not found in blocks.hard_gates.rules")

    _save_yaml(copy_path, obj)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    baseline = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    copy_path = repo_root / "args" / "data" / "invariants_hg_v0_copy.yaml"

    print("NEGATIVE_TEST: reset copy-policy to baseline")
    _reset_copy(baseline, copy_path)

    print("NEGATIVE_TEST: flip data_integrity_gate decision UNKNOWN->ALLOW in copy-policy")
    _flip_data_integrity_gate_to_allow(copy_path)

    print("NEGATIVE_TEST: run meta_audit (expect FAIL + exit_code=2)")
    report = run_meta_audit(str(baseline), str(copy_path))
    s = report["summary"]
    print(f"META_AUDIT FAIL={s['FAIL']} WARNING={s['WARNING']} INFO={s['INFO']}")

    exit_code = 2 if s["FAIL"] > 0 else 0
    print(f"DEMO_META_AUDIT_NEGATIVE exit_code={exit_code}")

    # Always reset copy-policy back
    print("NEGATIVE_TEST: reset copy-policy back to baseline")
    _reset_copy(baseline, copy_path)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
