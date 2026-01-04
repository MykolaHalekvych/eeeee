"""
Demo runner for policy diff audit.

Compares:
- args/data/invariants_hg_v0.yaml
- args/data/invariants_hg_v0_copy.yaml

Prints a compact summary + counts.
"""

from pathlib import Path

from args.audit.policy_diff import diff_policies


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    root = _repo_root()
    old_path = root / "args" / "data" / "invariants_hg_v0.yaml"
    new_path = root / "args" / "data" / "invariants_hg_v0_copy.yaml"

    report = diff_policies(str(old_path), str(new_path))

    print("POLICY DIFF")
    print("old:", report["policy_names"]["old"])
    print("new:", report["policy_names"]["new"])
    print()

    ds = report["decision_set_allowed"]
    rl = report["risk_limits"]
    impact = report["impact_summary"]

    print("decision_set.allowed diff:", ds)
    print("risk_envelope.limits diff counts:", {k: len(v) for k, v in rl.items()})
    print("impact_summary:", impact)

    # If there are any rule changes, show per-block counts
    if impact["rule_changes_total"] > 0:
        print()
        print("rule changes by block:")
        for b, info in report["rules"].items():
            c = info["counts"]
            if c["added"] or c["removed"] or c["changed"]:
                print(f"- {b}: {c}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
