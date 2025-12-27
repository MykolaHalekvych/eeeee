from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from args.wa.wa_order_gateway_v1 import append_jsonl, enforce_mode_gate, iter_jsonl


# Stage A: semantic kinds
KIND_NONE = "INTENT_NONE"
KIND_CANCEL_ALL = "INTENT_CANCEL_ALL"
KIND_ENTRY = "INTENT_ENTRY"
KIND_EXIT = "INTENT_EXIT"
KIND_REDUCE = "INTENT_REDUCE"
KIND_TAKE_PROFIT = "INTENT_TAKE_PROFIT"

_ALLOWED_KINDS = {KIND_NONE, KIND_CANCEL_ALL, KIND_ENTRY, KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}


def _s(x: Any, default: str = "") -> str:
    if x is None:
        return default
    v = str(x).strip()
    return v if v else default


def _i(x: Any) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None


def _norm_kind(x: Any) -> str:
    k = str(x or "").strip().upper().replace("-", "_")
    return k if k in _ALLOWED_KINDS else KIND_NONE


def normalize_order_intent(intent: Dict[str, Any], run_report: Dict[str, Any], source: str = "wa_v1") -> Dict[str, Any]:
    """
    Contract record for order_intents_v1 (stable, minimal, auditable).
    Stage A: semantic kinds + strict run_id.
    """
    run_id = _s(intent.get("run_id") or run_report.get("run_id"), "")
    if not run_id:
        raise ValueError("order_intents_v1: run_id missing (intent/run_report)")

    instrument = _s(intent.get("instrument"), _s(run_report.get("instrument"), "HG"))
    timeframe = _s(intent.get("timeframe"), _s(run_report.get("timeframe"), "5m"))

    rec: Dict[str, Any] = {
        "schema": "order_intents_v1",
        "source": source,
        "run_id": run_id,
        "index": _i(intent.get("index")),
        "ts": intent.get("ts"),  # keep as-is (None/str/number)
        "instrument": instrument,
        "timeframe": timeframe,
        "ma_decision": intent.get("ma_decision"),

        # audit fields from Stage 4.2 gate
        "mode": intent.get("mode"),
        "position_size": intent.get("position_size"),
        "kind_raw": intent.get("kind_raw"),
        "kind": _norm_kind(intent.get("kind")),
        "gate_reason": intent.get("gate_reason"),
    }

    if rec["gate_reason"] in ("", None):
        rec["gate_reason"] = None

    return rec


def build_order_intents(run_report: Dict[str, Any], raw_intents_path: Path, out_path: Path, source: str = "wa_v1") -> Dict[str, Any]:
    """
    Reads raw intents JSONL -> enforces mode gate -> writes normalized order_intents JSONL.
    """
    if out_path.exists():
        out_path.unlink()

    total = 0
    gated = 0
    allowed = 0
    none = 0
    cancel_all = 0
    exits = 0
    entries = 0

    for raw in iter_jsonl(raw_intents_path):
        total += 1

        # defense-in-depth: derive mode/position from report, then gate
        gated_intent = enforce_mode_gate(raw, run_report)

        k = _norm_kind(gated_intent.get("kind"))

        if k == KIND_NONE:
            none += 1
            if gated_intent.get("gate_reason"):
                gated += 1
        else:
            allowed += 1
            if k == KIND_CANCEL_ALL:
                cancel_all += 1
            elif k in {KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}:
                exits += 1
            elif k == KIND_ENTRY:
                entries += 1

        rec = normalize_order_intent(gated_intent, run_report, source=source)
        append_jsonl(out_path, rec)

    return {
        "raw_intents": str(raw_intents_path),
        "order_intents": str(out_path),
        "total": total,
        "allowed": allowed,
        "none": none,
        "gated": gated,
        "breakdown": {
            "entries": entries,
            "exits": exits,
            "cancel_all": cancel_all,
        },
    }
