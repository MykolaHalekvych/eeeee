from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from args.wa.wa_order_gateway_v1 import decide_intent, iter_jsonl, append_jsonl


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

    outputs = report.get("outputs") if isinstance(report.get("outputs"), dict) else {}
    orders_path = outputs.get("orders_paper")

    if not run_id:
        print(f"FAIL: run_id missing in {rp}")
        return 3
    if not orders_path:
        print(f"FAIL: outputs.orders_paper missing in {rp}")
        return 4

    orders_file = Path(str(orders_path))
    if not orders_file.is_absolute():
        orders_file = REPO_ROOT / orders_file

    if not orders_file.exists():
        print(f"FAIL: orders file not found: {orders_file}")
        return 5

    # Optional halt signal (best-effort)
    hsum = report.get("harness_summary") if isinstance(report.get("harness_summary"), dict) else {}
    halted = bool(hsum.get("halted")) if isinstance(hsum.get("halted"), (bool, int)) else False
    halt_reason = str(hsum.get("halt_reason") or "")

    out_intents = DATA_DIR / f"orders_intent_{run_id}.jsonl"
    if out_intents.exists():
        out_intents.unlink()

    total = 0
    n_none = 0
    n_order = 0
    n_cancel = 0

    if halted:
        intent = decide_intent(
            run_id=run_id,
            index=None,
            ts=None,
            instrument="HG",
            timeframe="5m",
            ma_decision="UNKNOWN",
            wa_action={},
            halted=True,
            halt_reason=halt_reason,
        )
        append_jsonl(out_intents, intent.to_dict())
        print("WA_V1_INTENTS")
        print(f"latest_report: {rp}")
        print(f"run_id: {run_id}")
        print(f"orders_in: {orders_file}")
        print(f"out_intents: {out_intents}")
        print("halted: True")
        print(json.dumps({"intent_cancel_all": 1, "intent_none": 0, "intent_order": 0}, ensure_ascii=False, indent=2))
        return 0

    for row in iter_jsonl(orders_file):
        if row.get("kind") != "ORDER_PAPER":
            continue
        total += 1

        wa_action = row.get("wa_action")
        if not isinstance(wa_action, dict):
            wa_action = {}

        intent = decide_intent(
            run_id=run_id,
            index=row.get("index"),
            ts=row.get("ts"),
            instrument=str(row.get("instrument") or "HG"),
            timeframe=str(row.get("timeframe") or "5m"),
            ma_decision=row.get("ma_decision"),
            wa_action=wa_action,
            halted=False,
            halt_reason="",
        )
        append_jsonl(out_intents, intent.to_dict())

        if intent.kind == "INTENT_NONE":
            n_none += 1
        elif intent.kind == "INTENT_ORDER":
            n_order += 1
        else:
            n_cancel += 1

    summary = {
        "orders_in_total": total,
        "intent_none": n_none,
        "intent_order": n_order,
        "intent_cancel_all": n_cancel,
        "out_intents": str(out_intents),
    }

    print("WA_V1_INTENTS")
    print(f"latest_report: {rp}")
    print(f"run_id: {run_id}")
    print(f"orders_in: {orders_file}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
