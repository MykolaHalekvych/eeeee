from __future__ import annotations

from pathlib import Path

from args.audit.meta_audit import run_meta_audit, _print_human


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]

    baseline = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    candidate = repo_root / "args" / "data" / "invariants_hg_v0_copy.yaml"

    report = run_meta_audit(str(baseline), str(candidate))
    _print_human(report)

    exit_code = 2 if report["summary"]["FAIL"] > 0 else 0
    print(f"DEMO_META_AUDIT exit_code={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
