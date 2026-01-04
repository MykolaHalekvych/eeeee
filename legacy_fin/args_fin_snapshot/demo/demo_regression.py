"""
Demo runner for regression set.

- Loads all JSON cases from args/data/regression_cases
- Runs MA evaluation and compares with expected outputs
- Prints TOTAL / PASS / FAIL and lists failed cases

EXIT CODES:
- 0 if failed == 0
- 2 if failed > 0
"""

from pathlib import Path

from args.audit.regression_runner import run_regression


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> int:
    root = _repo_root()

    cases_dir = root / "args" / "data" / "regression_cases"
    policy_path = root / "args" / "data" / "invariants_hg_v0.yaml"

    case_paths = sorted([str(p) for p in cases_dir.glob("case_*.json")])

    report = run_regression(case_paths, str(policy_path))

    print(f"REGRESSION total={report['total']} passed={report['passed']} failed={report['failed']}")

    if report["failed"] > 0:
        print("FAILED CASES:")
        for r in report["results"]:
            if not r["passed"]:
                print(f"- {r['case_name']}: {r['reason']} expected={r['expected']} actual={r['actual']}")

    return 2 if report["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
