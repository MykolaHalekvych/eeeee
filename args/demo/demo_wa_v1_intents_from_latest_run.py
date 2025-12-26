from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from args.wa.order_intents_v1 import build_order_intents
from args.wa.wa_order_gateway_v1 import append_jsonl, decide_intent, enforce_mode_gate, iter_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

# DEBUG (OFF by default). Enable only for proofs.
DEBUG_FORCE_ACTION = ""  # e.g. "ENTER_LONG"
DEBUG_FORCE_KIND = ""    # e.g. "INTENT_ORDER"


def _latest_report() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    reports = sorted(
        [
            p
            for p in LOGS_DIR.iterdir()
            if p.is_file() and p.name.startswith("run_report_") and p.name.endswith("_paper.json")
        ],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_to_repo(p: Any) -> Path:
    pp = Path(str(p))
    return pp if pp.is_absolute() else (REPO_ROOT / pp)


def main() -> int:
    rp = _latest_report()
    if rp is None:
        print("FAIL: no run_report_*_paper.json found in args/logs")
        return 2

    report = _load_json(rp)
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        print(f"FAIL: run_id missing in {rp}")
        return 3

    outputs = report.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}

    orders_path = outputs.get("orders_paper")
    if not orders_path:
        print(f"FAIL: outputs.orders_paper missing in {rp}")
        return 4

    orders_file = _resolve_to_repo(orders_path)
    if not orders_file.exists():
        print(f"FAIL: orders file not found: {orders_file}")
        return 5

    # Raw intents output (legacy)
    out_intents = DATA_DIR / f"orders_intent_{run_id}.jsonl"
    if out_intents.exists():
        out_intents.unlink()

    total = 0
    n_none = 0
    n_order = 0
    n_cancel = 0
    n_gated = 0

    for row in iter_jsonl(orders_file):
        if row.get("kind") != "ORDER_PAPER":
            continue
        total += 1

        wa_action = row.get("wa_action")
        if not isinstance(wa_action, dict):
            wa_action = {}

        # Optional debug: annotate action (for visibility only)
        if DEBUG_FORCE_ACTION:
            wa_action = dict(wa_action)
            wa_action.setdefault("intent", str(DEBUG_FORCE_ACTION).strip())
            wa_action["debug_force_action"] = str(DEBUG_FORCE_ACTION).strip()

        ma_decision_row = row.get("ma_decision")

        intent_obj = decide_intent(
            run_id=run_id,
            index=row.get("index"),
            ts=row.get("ts"),
            instrument=str(row.get("instrument") or "HG"),
            timeframe=str(row.get("timeframe") or "5m"),
            ma_decision=ma_decision_row,
            wa_action=wa_action,
            halted=False,
            halt_reason="",
        )

        d = intent_obj.to_dict()

        # Preserve original kind as kind_raw BEFORE gating (and allow debug override)
        original_kind = str(d.get("kind") or "").strip()
        d["kind_raw"] = original_kind if original_kind else d.get("kind_raw")

        if DEBUG_FORCE_KIND:
            d["kind_raw"] = str(DEBUG_FORCE_KIND).strip()
            d["kind"] = str(DEBUG_FORCE_KIND).strip()
            d["debug_force_kind"] = str(DEBUG_FORCE_KIND).strip()

        # Stage 4.2 gate (single source of truth)
        d = enforce_mode_gate(d, report)

        k = str(d.get("kind") or "").strip().upper()
        if k == "INTENT_NONE":
            n_none += 1
            if d.get("gate_reason"):
                n_gated += 1
        elif k == "INTENT_ORDER":
            n_order += 1
        elif k == "INTENT_CANCEL_ALL":
            n_cancel += 1
        else:
            # Unknown kinds are treated as NONE for safety
            n_none += 1
            d["kind"] = "INTENT_NONE"
            d["gate_reason"] = d.get("gate_reason") or "unknown_kind_safety_none"

        append_jsonl(out_intents, d)

    # Stage 4.3: Contract artifact (order_intents_<run_id>.jsonl)
    order_intents_path = DATA_DIR / f"order_intents_{run_id}.jsonl"
    oi = build_order_intents(report, out_intents, order_intents_path, source="wa_v1")

    summary = {
        "latest_report": str(rp),
        "run_id": run_id,
        "orders_in": str(orders_file),

        "orders_in_total": total,
        "intent_none": n_none,
        "intent_order": n_order,
        "intent_cancel_all": n_cancel,
        "gated_total": n_gated,

        "out_intents": str(out_intents),
        "order_intents_out": str(order_intents_path),
        "order_intents_total": oi.get("total"),
        "order_intents_allowed": oi.get("allowed"),
        "order_intents_none": oi.get("none"),
        "order_intents_gated": oi.get("gated"),

        "debug_force_action": DEBUG_FORCE_ACTION,
        "debug_force_kind": DEBUG_FORCE_KIND,
    }

    print("WA_V1_INTENTS")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


