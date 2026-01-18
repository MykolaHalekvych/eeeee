from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from args.ibkr.ibkr_sender_real_v1 import real_sender

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _latest_report() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    reports = sorted(
        [
            p
            for p in LOGS_DIR.iterdir()
            if p.is_file()
            and p.name.startswith("run_report_")
            and p.name.endswith("_paper.json")
        ],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    rp = _latest_report()
    if rp is None:
        print("FAIL: no run_report_*_paper.json found in args/logs")
        return 2

    report = _load_json(rp)
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        print(f"FAIL: run_id missing in {rp}")
        return 3

    sendplan_path = DATA_DIR / f"orders_sendplan_{run_id}.jsonl"
    if not sendplan_path.exists():
        print(f"FAIL: sendplan missing: {sendplan_path}")
        print("Run sender dryrun first:")
        print("  py -3.11 -m args.demo.demo_ibkr_sender_dryrun_from_latest_run")
        return 4

    control_state_path = DATA_DIR / "control_state.json"

    out = real_sender(
        sendplan_path=sendplan_path,
        run_report_path=rp,
        control_state_path=control_state_path,
    )

    print("IBKR_SENDER_REAL_V1")
    print(json.dumps({"latest_report": str(rp), **out}, ensure_ascii=False, indent=2))

    # Exit codes:
    # - 0 if no errors and DISARMED (expected by default)
    # - 2 if any errors_count > 0 or parse_errors > 0
    if int(out.get("errors_count") or 0) > 0 or int(out.get("parse_errors") or 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
