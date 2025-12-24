"""
Demo for ARGS Core MA evaluation.

- Loads policy from args/data/invariants_hg_v0.yaml
- Builds a synthetic ctx with:
  - risk.margin_usage above threshold
  - state.tail_risk = UNKNOWN
  - state.confidence = 0.62
  - data.qc = OK
- Runs eval_ma(policy, ctx)
- Prints decision + violations
- Appends an event to args/data/events.jsonl
"""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from args.ma.policy_loader import load_policy
from args.ma.ma_runtime import eval_ma
from args.audit.event_store import append_event


def _repo_root() -> Path:
    # args/demo/demo_ma_eval.py -> repo root
    return Path(__file__).resolve().parents[2]


def main() -> int:
    root = _repo_root()
    policy_path = root / "args" / "data" / "invariants_hg_v0.yaml"
    events_path = root / "args" / "data" / "events.jsonl"

    policy = load_policy(str(policy_path))

    # Synthetic ctx (labels only)
    ctx = {
        "instrument": "HG",
        "timeframe": "5m",
        "env": "IBKR_PAPER_LABEL",
        "data": {
            "qc": "OK",
            "missing_bars": 0,
            "stale_quotes": False,
            "timestamp_drift_ms": 0,
        },
        "state": {
            "regime": "TREND",
            "confidence": 0.62,
            "tail_risk": "UNKNOWN",
            "liquidity": "NORMAL",
        },
        "risk": {
            "margin_usage": 0.41,
        },
        "exec": {
            "kill_switch": False,
        },
        "pnl": {"unrealized": 0.0, "realized": 0.0},
        "stats": {},
    }

    result = eval_ma(policy, ctx)

    print("MA decision:", result["ma_decision"])
    if result["violations"]:
        print("Violations:")
        for v in result["violations"]:
            print(f"  - {v['rule_id']} [{v['block']}]: {v['reason']}")

    event = {
        "event_id": str(uuid4()),
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "policy_name": policy.name,
        "schema_version": policy.schema_version,
        "instrument": ctx["instrument"],
        "timeframe": ctx["timeframe"],
        "environment": ctx["env"],
        "ma_decision": result["ma_decision"],
        "violations": result["violations"],
        "risk_envelope": result["risk_envelope"],
        "ctx_snapshot": {
            "data": ctx.get("data", {}),
            "state": ctx.get("state", {}),
            "risk": ctx.get("risk", {}),
        },
    }

    append_event(str(events_path), event)
    print("Appended event to:", events_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
