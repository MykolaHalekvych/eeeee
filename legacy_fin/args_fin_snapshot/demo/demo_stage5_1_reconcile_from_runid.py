# args/demo/demo_stage5_1_reconcile_from_runid.py
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"


def _run(mod: str, args: list[str]) -> None:
    cmd = [sys.executable, "-m", mod, *args]
    r = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage5.1 demo: snapshots + reconcile")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--timeout-s", type=float, default=20.0)
    ap.add_argument("--lookback-min", type=int, default=1440)
    a = ap.parse_args()

    run_id = str(a.run_id).strip()

    # 1) account snapshot
    _run(
        "args.ibkr.ibkr_account_snapshot_v0",
        [
            "--host",
            a.host,
            "--port",
            str(a.port),
            "--client-id",
            str(a.client_id),
            "--timeout-s",
            str(a.timeout_s),
            "--run-id",
            run_id,
        ],
    )

    # 2) positions snapshot
    _run(
        "args.ibkr.ibkr_positions_snapshot_v0",
        [
            "--host",
            a.host,
            "--port",
            str(a.port),
            "--client-id",
            str(a.client_id),
            "--timeout-s",
            str(a.timeout_s),
            "--run-id",
            run_id,
        ],
    )

    # 3) open orders snapshot (existing tool)
    out_open = DATA_DIR / "ibkr_open_orders_stage5.jsonl"
    _run(
        "args.ibkr.ibkr_open_orders_snapshotter_v0",
        [
            "--host",
            a.host,
            "--port",
            str(a.port),
            "--client-id",
            str(a.client_id),
            "--timeout-s",
            "25",
            "--wait-s",
            "5",
            "--out",
            str(out_open),
        ],
    )

    # 4) executions snapshot
    _run(
        "args.ibkr.ibkr_executions_snapshot_v0",
        [
            "--host",
            a.host,
            "--port",
            str(a.port),
            "--client-id",
            str(a.client_id),
            "--timeout-s",
            str(a.timeout_s),
            "--run-id",
            run_id,
            "--lookback-min",
            str(a.lookback_min),
        ],
    )

    # 5) reconcile
    _run("args.wa.reconcile_paper_v1", ["--run-id", run_id])

    print(
        json.dumps(
            {"ok": True, "run_id": run_id, "note": "stage5.1 snapshots+reconcile done"},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
