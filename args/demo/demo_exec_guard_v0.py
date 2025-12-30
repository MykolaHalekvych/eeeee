from __future__ import annotations

import json
import uuid
import datetime as dt
from pathlib import Path

from args.control.execution_mode_v0 import (
    get_execution_mode,
    is_stop_flag_present,
    is_action_allowed,
    apply_block_to_sendplan_inplace,
    mk_exec_event,
    append_exec_event,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODE_PATH = REPO_ROOT / "args" / "data" / "execution_mode.json"


def _write_mode(mode: str) -> None:
    MODE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODE_PATH.write_text(
        json.dumps(
            {
                "mode": mode,
                "updated_ts_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                "note": "demo_exec_guard_v0",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    run_id = "guardtest_" + uuid.uuid4().hex[:8]

    item = {
        "run_id": run_id,
        "intent_kind": "ENTER",
        "ma_decision": "NO_TRADE",
        "gate_reason": "ENFORCED_NO_TRADE: state_unknown_gate, margin_usage_gate, tail_unknown_gate",
        "plan_kind": "PLAN_IBKR_PLACE_ORDER",
        "payload_kind": "PAYLOAD_IBKR_ORDER",
        "payload_execute": True,
    }

    for mode in ("DRY_RUN", "EXIT_ONLY", "FULL"):
        _write_mode(mode)
        eff_mode = get_execution_mode(REPO_ROOT)
        stop_flag = is_stop_flag_present(REPO_ROOT)

        allowed, why = is_action_allowed(
            mode=eff_mode,
            stop_flag=stop_flag,
            ma_decision=item["ma_decision"],
            gate_reason=item["gate_reason"],
            intent_kind=item["intent_kind"],
            plan_kind=item["plan_kind"],
            payload_kind=item["payload_kind"],
            sendplan_item=item,
        )

        print(f"[{mode}] allowed={allowed} reason={why}")

        tmp = dict(item)
        if not allowed:
            apply_block_to_sendplan_inplace(tmp, why, f"demo blocked; mode={mode}")

        ev = mk_exec_event(
            kind="DEMO_GUARD_RESULT",
            run_id=run_id,
            outcome="ALLOW" if allowed else "BLOCKED",
            mode=eff_mode,
            reason_code=why,
            sendplan_item=tmp,
            details={"mode_tested": mode},
        )
        append_exec_event(REPO_ROOT, run_id, ev)

    print(f"OK. Wrote demo exec events: args/data/orders_exec_events_{run_id}.jsonl")


if __name__ == "__main__":
    main()
