from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from args.ibkr.ibkr_order_payload_v1 import build_contract_ref, intent_to_payload
from args.wa.wa_order_gateway_v1 import append_jsonl, decide_intent, enforce_mode_gate, iter_jsonl

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _latest_report() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    reports = sorted(
        [
            p
            for p in LOGS_DIR.iterdir()
            if p.is_file()
            and p.name.startswith("run_report_")
            and p.name.endswith("_paper.json")
        ],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_path(p: Any) -> Path:
    pp = Path(str(p))
    return pp if pp.is_absolute() else (REPO_ROOT / pp)


def _load_contract_meta_from_report(report: Dict[str, Any]) -> Dict[str, Any]:
    inp = report.get("inputs")
    if not isinstance(inp, dict):
        raise ValueError("run_report.inputs missing")

    # Prefer embedded csv_meta.contract
    csv_meta = inp.get("csv_meta")
    if isinstance(csv_meta, dict) and isinstance(csv_meta.get("contract"), dict):
        return csv_meta["contract"]

    # Fallback: read csv_meta_path if present
    meta_path = inp.get("csv_meta_path")
    if meta_path:
        mp = _resolve_path(meta_path)
        if mp.exists():
            obj = _load_json(mp)
            c = obj.get("contract")
            if isinstance(c, dict):
                return c

    raise ValueError("No contract meta found in run_report (csv_meta / csv_meta_path)")


def _ensure_intents(report: Dict[str, Any]) -> Tuple[str, Path]:
    run_id = str(report.get("run_id") or "")
    if not run_id:
        raise ValueError("run_id missing in report")

    intents_path = DATA_DIR / f"orders_intent_{run_id}.jsonl"
    if intents_path.exists():
        return run_id, intents_path

    # Build intents from orders_paper (same logic as Stage23A demo, but embedded here)
    out = report.get("outputs")
    if not isinstance(out, dict):
        raise ValueError("report.outputs missing")
    orders_paper = out.get("orders_paper")
    if not orders_paper:
        raise ValueError("report.outputs.orders_paper missing")
    orders_file = _resolve_path(orders_paper)
    if not orders_file.exists():
        raise ValueError(f"orders_paper file missing: {orders_file}")

    # Optional halt flag
    hsum = report.get("harness_summary")
    halted = False
    halt_reason = ""
    if isinstance(hsum, dict):
        halted = bool(hsum.get("halted")) if isinstance(hsum.get("halted"), (bool, int)) else False
        halt_reason = str(hsum.get("halt_reason") or "")

    if intents_path.exists():
        intents_path.unlink()

    if halted:
        intent_obj = decide_intent(
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
        append_jsonl(intents_path, intent_obj.to_dict())
        return run_id, intents_path

    for row in iter_jsonl(orders_file):
        if row.get("kind") != "ORDER_PAPER":
            continue

        wa_action = row.get("wa_action")
        if not isinstance(wa_action, dict):
            wa_action = {}

        intent_obj = decide_intent(
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
        append_jsonl(intents_path, intent_obj.to_dict())

    return run_id, intents_path


def main() -> int:
    rp = _latest_report()
    if rp is None:
        print("FAIL: no run_report_*_paper.json found in args/logs")
        return 2

    report = _load_json(rp)
    run_id, intents_path = _ensure_intents(report)

    contract_meta = _load_contract_meta_from_report(report)
    contract_ref = build_contract_ref(contract_meta)

    out_payload = DATA_DIR / f"orders_payload_{run_id}.jsonl"
    if out_payload.exists():
        out_payload.unlink()

    n_total = 0
    n_none = 0
    n_order = 0
    n_cancel = 0
    n_gated = 0

    for intent in iter_jsonl(intents_path):
        n_total += 1

        # Stage 4.2: enforce mode-gating INSIDE payload pipeline (defense-in-depth)
        intent = enforce_mode_gate(intent, report)
        if intent.get("kind") == "INTENT_NONE" and intent.get("gate_reason"):
            n_gated += 1

        payload = intent_to_payload(intent, contract_ref, dry_run=True)

        # Attach audit fields (safe extra keys)
        payload["mode"] = intent.get("mode")
        payload["position_size"] = intent.get("position_size")
        payload["intent_kind_raw"] = intent.get("kind_raw")
        payload["intent_kind"] = intent.get("kind")
        if intent.get("gate_reason"):
            payload["gate_reason"] = intent.get("gate_reason")

        append_jsonl(out_payload, payload)

        k = payload.get("payload_kind")
        if k == "PAYLOAD_NONE":
            n_none += 1
        elif k == "PAYLOAD_ORDER":
            n_order += 1
        elif k == "PAYLOAD_CANCEL_ALL":
            n_cancel += 1

    summary = {
        "latest_report": str(rp),
        "run_id": run_id,
        "intents_in": str(intents_path),
        "payload_out": str(out_payload),
        "payload_total": n_total,
        "payload_none": n_none,
        "payload_order": n_order,
        "payload_cancel_all": n_cancel,
        "gated_total": n_gated,
        "contract": {
            "conId": contract_ref.get("conId"),
            "localSymbol": contract_ref.get("localSymbol"),
        },
    }

    print("WA_V1_PAYLOAD_DRYRUN")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
