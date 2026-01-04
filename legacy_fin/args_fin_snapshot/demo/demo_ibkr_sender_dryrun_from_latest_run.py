from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from args.ibkr.ibkr_sender_dryrun_v1 import dryrun_sender

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _latest_report() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    reports = sorted(
        [p for p in LOGS_DIR.iterdir() if p.is_file() and p.name.startswith("run_report_") and p.name.endswith("_paper.json")],
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
    run_id = str(report.get("run_id") or "")
    if not run_id:
        print(f"FAIL: run_id missing in {rp}")
        return 3

    payload_path = DATA_DIR / f"orders_payload_{run_id}.jsonl"
    if not payload_path.exists():
        print(f"FAIL: payload file missing: {payload_path}")
        print("Run Stage23B payload demo first:")
        print("  py -3.11 -m args.demo.demo_wa_v1_payload_from_latest_run")
        return 4

    out_sendplan = DATA_DIR / f"orders_sendplan_{run_id}.jsonl"
    summary = dryrun_sender(payload_path=payload_path, out_sendplan_path=out_sendplan)

    print("IBKR_SENDER_DRYRUN_V1")
    print(json.dumps({"latest_report": str(rp), "run_id": run_id, **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
