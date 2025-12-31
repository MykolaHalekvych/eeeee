from __future__ import annotations

import argparse
import json
from pathlib import Path

from args.ibkr.ibkr_sender_real_v1 import real_sender


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()

    run_id = a.run_id.strip()

    sendplan_path = DATA_DIR / f"orders_sendplan_{run_id}.jsonl"
    run_report_path = LOGS_DIR / f"run_report_{run_id}_paper.json"
    control_state_path = DATA_DIR / "control_state.json"

    out = real_sender(
        sendplan_path=sendplan_path,
        run_report_path=run_report_path,
        control_state_path=control_state_path,
    )

    print("STAGE5_SENDER_FROM_RUNID")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
