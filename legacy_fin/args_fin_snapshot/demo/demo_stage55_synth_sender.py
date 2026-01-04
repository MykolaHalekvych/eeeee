from __future__ import annotations

import json
import sys
from pathlib import Path

from args.ibkr.ibkr_sender_real_v1 import real_sender, DATA_DIR


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    run_id = "stage55_synth"

    sendplan_path = DATA_DIR / f"orders_sendplan_{run_id}.jsonl"
    run_report_path = DATA_DIR / f"run_report_{run_id}_paper.json"
    control_state_path = DATA_DIR / "control_state.json"

    # Minimal run_report so extract_run_mode => ALLOW_NEW_ENTRIES
    write_json(
        run_report_path,
        {
            "run_id": run_id,
            "risk_envelope": {"mode": "ALLOW_NEW_ENTRIES"},
        },
    )

    # Minimal SENDPLAN_ORDER that passes your validation
    rows = [
        {
            "kind": "SENDPLAN_ORDER",
            "run_id": run_id,
            "idempotency_key": "SYNTH-ORDER-1",
            "contract": {"conId": 123, "localSymbol": "MHG", "secType": "FUT", "exchange": "COMEX", "currency": "USD"},
            "order": {"action": "BUY", "orderType": "MKT", "totalQuantity": 1, "tif": "DAY", "transmit": False},
            "reason": "stage55_synth",
        }
    ]
    write_jsonl(sendplan_path, rows)

    print("=== Stage 5.5 synth run #1 ===")
    r1 = real_sender(sendplan_path=sendplan_path, run_report_path=run_report_path, control_state_path=control_state_path)
    print(json.dumps(r1, ensure_ascii=False, indent=2))

    print("\n=== Stage 5.5 synth run #2 (should DEDUPE) ===")
    r2 = real_sender(sendplan_path=sendplan_path, run_report_path=run_report_path, control_state_path=control_state_path)
    print(json.dumps(r2, ensure_ascii=False, indent=2))

    # Assertions
    if not r1.get("simulate"):
        print("FAIL: expected simulate=true", file=sys.stderr)
        sys.exit(2)

    if int(r1.get("executed_orders") or 0) < 1:
        print("FAIL: run #1 must send >=1 in simulate mode", file=sys.stderr)
        sys.exit(2)

    if int(r2.get("executed_orders") or 0) != 0:
        print("FAIL: run #2 must send 0 due to dedupe", file=sys.stderr)
        sys.exit(2)

    print("\nPASS: dedupe works in simulate mode.")


if __name__ == "__main__":
    main()
