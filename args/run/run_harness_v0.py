from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from args.asys.as_v0_adapter import parse_bar_row, derive_ma_input_from_bar
from args.contracts.paths_from_policy import inventory_from_policy_yaml
from args.contracts.ma_input_contract import validate_ma_input
from args.ma.policy_loader import load_policy
from args.ma.ma_runtime import eval_ma


@dataclass(frozen=True)
class RunConfig:
    csv_path: Path
    policy_path: Path
    out_events: Path
    max_steps: int = 0  # 0 = all rows
    start_index: int = 0


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.write("\n")


def _load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_harness(cfg: RunConfig) -> Dict[str, Any]:
    rows = _load_rows(cfg.csv_path)
    total_rows = len(rows)

    if cfg.start_index < 0 or cfg.start_index > total_rows:
        raise ValueError("start_index out of range")

    # limit rows if max_steps > 0
    end_index = total_rows
    if cfg.max_steps and cfg.max_steps > 0:
        end_index = min(total_rows, cfg.start_index + cfg.max_steps)

    slice_rows = rows[cfg.start_index:end_index]

    # prepare policy + contract inventory once
    policy = load_policy(str(cfg.policy_path))
    inv = inventory_from_policy_yaml(str(cfg.policy_path))

    summary = {
        "total_rows": total_rows,
        "processed": 0,
        "contract_fail": 0,
        "ma_decisions": {},
        "out_events": str(cfg.out_events),
    }

    _append_jsonl(
        cfg.out_events,
        {"kind": "RUN_START", "csv": str(cfg.csv_path), "policy": str(cfg.policy_path), "start_index": cfg.start_index, "end_index": end_index},
    )

    for i, r in enumerate(slice_rows, start=cfg.start_index):
        bar = parse_bar_row(r)

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

        # required fields gate
        ma_input["instrument"] = "HG"
        ma_input["timeframe"] = "5m"
        ma_input["env"] = "IBKR_PAPER_LABEL"

        # contract check
        res = validate_ma_input(ma_input, inv.paths)
        if not res.ok:
            summary["contract_fail"] += 1
            _append_jsonl(
                cfg.out_events,
                {
                    "kind": "CONTRACT_FAIL",
                    "index": i,
                    "ts": bar.ts,
                    "missing_paths": res.missing_paths,
                    "unknown_top_level_keys": res.unknown_top_level_keys,
                },
            )
            continue

        report = eval_ma(policy, ma_input)
        decision = report.get("ma_decision", "UNKNOWN")

        summary["processed"] += 1
        summary["ma_decisions"][decision] = summary["ma_decisions"].get(decision, 0) + 1

        _append_jsonl(
            cfg.out_events,
            {
                "kind": "TICK",
                "index": i,
                "ts": bar.ts,
                "bar": {"ts": bar.ts, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume},
                "ma_decision": decision,
                "violations": report.get("violations", []),
            },
        )

    _append_jsonl(cfg.out_events, {"kind": "RUN_DONE", "summary": summary})
    return summary


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = RunConfig(
        csv_path=repo_root / "args" / "data" / "hg_5m_bars_sample.csv",
        policy_path=repo_root / "args" / "data" / "invariants_hg_v0.yaml",
        out_events=repo_root / "args" / "data" / "events_run.jsonl",
        max_steps=0,
        start_index=0,
    )

    summary = run_harness(cfg)
    print("RUN_SUMMARY:", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
