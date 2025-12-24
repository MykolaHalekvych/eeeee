from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from args.asys.as_v0_adapter import derive_ma_input_from_bar, parse_bar_row, pick_latest_bar
from args.contracts.ma_input_contract import validate_ma_input
from args.contracts.paths_from_policy import inventory_from_policy_yaml
from args.ma.ma_runtime import eval_ma
from args.ma.policy_loader import load_policy


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.write("\n")


def _load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]

    policy_path = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    csv_path = repo_root / "args" / "data" / "hg_5m_bars_sample.csv"
    out_events = repo_root / "args" / "data" / "events_as.jsonl"

    rows = _load_rows(csv_path)
    if not rows:
        print("ERROR: no rows in CSV:", csv_path)
        return 2

    bars = [parse_bar_row(r) for r in rows]
    bar = pick_latest_bar(bars)
    if bar is None:
        print("ERROR: no bar parsed:", csv_path)
        return 2

    ma_input = derive_ma_input_from_bar(
        bar,
        qc="OK",
        stale_quotes=False,
        missing_bars=0,
        timestamp_drift_ms=0,
        kill_switch=False,
        liquidity=1.0,
        margin_usage=0.40,
        confidence=0.55,
        regime="UNKNOWN",
        tail_risk="UNKNOWN",
        correlation=0.10,
    )

    # Required fields gate (MA runtime expects these)
    ma_input["instrument"] = "HG"
    ma_input["timeframe"] = "5m"
    ma_input["env"] = "IBKR_PAPER_LABEL"

    inv = inventory_from_policy_yaml(str(policy_path))
    res = validate_ma_input(ma_input, inv.paths)
    print("CONTRACT_OK:", res.ok, "MISSING:", len(res.missing_paths))
    if not res.ok:
        for p in res.missing_paths:
            print(" -", p)
        return 2

    policy = load_policy(str(policy_path))

    # Debug (optional, helps confirm required fields are present)
    print("DEBUG_FIELDS:", ma_input.get("instrument"), ma_input.get("timeframe"), ma_input.get("env"))

    report = eval_ma(policy, ma_input)

    print("MA decision:", report.get("ma_decision"))
    v = report.get("violations", [])
    print("Violations:", len(v))
    for it in v:
        print(" -", it)

    event = {
        "kind": "AS_V0_EVAL",
        "ts": bar.ts,
        "bar": {
            "ts": bar.ts,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        },
        "ma_input": ma_input,
        "ma_report": report,
    }
    _append_jsonl(out_events, event)
    print("Appended event to:", out_events)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
