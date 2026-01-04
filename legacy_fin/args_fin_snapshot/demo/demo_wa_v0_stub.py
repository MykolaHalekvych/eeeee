from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

from args.wa.wa_v0_stub import decide_wa_action


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    yield obj
            except json.JSONDecodeError:
                continue


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.write("\n")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    events_in = repo_root / "args" / "data" / "events_run.jsonl"
    orders_out = repo_root / "args" / "data" / "orders_paper.jsonl"

    n_in = 0
    n_ticks = 0
    n_out = 0

    for ev in _iter_jsonl(events_in):
        n_in += 1
        if ev.get("kind") != "TICK":
            continue

        n_ticks += 1
        ma_decision = ev.get("ma_decision")
        violations = ev.get("violations", [])
        if not isinstance(violations, list):
            violations = []

        action = decide_wa_action(ma_decision, violations)

        order = {
            "kind": "ORDER_PAPER",
            "source": "WA_V0",
            "index": ev.get("index"),
            "ts": ev.get("ts"),
            "instrument": "HG",
            "timeframe": "5m",
            "ma_decision": (ma_decision or "UNKNOWN"),
            "wa_action": action.to_dict(),
        }

        _append_jsonl(orders_out, order)
        n_out += 1

    print("WA_V0_STUB")
    print("events_in:", events_in)
    print("orders_out:", orders_out)
    print("seen_lines:", n_in)
    print("ticks:", n_ticks)
    print("orders_written:", n_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
