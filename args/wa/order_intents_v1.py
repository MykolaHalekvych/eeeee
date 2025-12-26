from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from args.wa.wa_order_gateway_v1 import append_jsonl, enforce_mode_gate, iter_jsonl


def _s(x: Any, default: str = "") -> str:
    if x is None:
        return default
    v = str(x)
    return v if v else default


def _i(x: Any) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None


def normalize_order_intent(intent: Dict[str, Any], run_report: Dict[str, Any], source: str = "wa_v1") -> Dict[str, Any]:
    """
    Contract record for order_intents_v1.
    Must be stable, minimal, auditable.
    """
    run_id = _s(intent.get("run_id") or run_report.get("run_id"), "")
    instrument = _s(intent.get("instrument") or "HG", "HG")
    timeframe = _s(intent.get("timeframe") or "5m", "5m")

    rec: Dict[str, Any] = {
        "schema": "order_intents_v1",
        "source": source,
        "run_id": run_id,
        "index": _i(intent.get("index")),
        "ts": intent.get("ts"),  # keep as-is (None/str/number) for now
        "instrument": instrument,
        "timeframe": timeframe,
        "ma_decision": intent.get("ma_decision"),

        # audit fields from Stage 4.2 gate
        "mode": intent.get("mode"),
        "position_size": intent.get("position_size"),
        "kind_raw": intent.get("kind_raw"),
        "kind": intent.get("kind"),
        "gate_reason": intent.get("gate_reason"),
    }

    # Normalize empties
    if rec["gate_reason"] in ("", None):
        rec["gate_reason"] = None

    return rec


def build_order_intents(run_report: Dict[str, Any], raw_intents_path: Path, out_path: Path, source: str = "wa_v1") -> Dict[str, Any]:
    """
    Reads raw intents jsonl -> enforces mode gate -> writes normalized order_intents jsonl.
    """
    if out_path.exists():
        out_path.unlink()

    total = 0
    gated = 0
    allowed = 0
    none = 0

    for raw in iter_jsonl(raw_intents_path):
        total += 1

        # defense-in-depth: derive mode/position from report, then gate
        gated_intent = enforce_mode_gate(raw, run_report)

        kind = gated_intent.get("kind")
        if kind == "INTENT_NONE":
            none += 1
            if gated_intent.get("gate_reason"):
                gated += 1
        else:
            allowed += 1

        rec = normalize_order_intent(gated_intent, run_report, source=source)
        append_jsonl(out_path, rec)

    return {
        "raw_intents": str(raw_intents_path),
        "order_intents": str(out_path),
        "total": total,
        "allowed": allowed,
        "none": none,
        "gated": gated,
    }
