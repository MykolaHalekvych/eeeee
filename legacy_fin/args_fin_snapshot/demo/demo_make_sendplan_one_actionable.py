from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"


def _read_first_jsonl(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
    raise RuntimeError(f"No dict lines in: {path}")


def main() -> int:
    # pick latest sendplan
    sendplans = sorted(
        DATA_DIR.glob("orders_sendplan_*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not sendplans:
        raise FileNotFoundError("No orders_sendplan_*.jsonl found in args/data")

    base_path = sendplans[0]
    base = _read_first_jsonl(base_path)

    run_id = str(base.get("run_id") or "synth")
    out_path = DATA_DIR / "orders_sendplan_stage55_synth.jsonl"

    # load contract mapping (HG only for now)
    contract_path = DATA_DIR / "ibkr_hg_contract_v1.json"
    if not contract_path.exists():
        raise FileNotFoundError(f"Missing contract file: {contract_path}")
    contract = json.loads(
        contract_path.read_text(encoding="utf-8-sig", errors="replace")
    )
    if not isinstance(contract, dict):
        raise RuntimeError("Contract json is not a dict")

    # minimal market order (paper safe; can still be rejected by Read-Only)
    order = {"action": "BUY", "totalQuantity": 1, "orderType": "MKT", "tif": "DAY"}

    plan = dict(base)
    plan.update(
        {
            "schema_version": "order_sendplan_v0",
            "run_id": run_id,
            "index": 0,
            "plan_kind": "PLAN_IBKR_PLACE_ORDER",
            "execute": True,
            "gate_reason": "FORCE_ONE_ACTIONABLE_SENDPLAN",
            "reason": "force_one_plan",
            "contract": contract,
            "order": order,
            # optional stability hook for ledger: keep constant per run
            "idempotency_key": f"force_one:{run_id}:HG:BUY:1:MKT:DAY",
        }
    )

    out_path.write_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"OK: wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
