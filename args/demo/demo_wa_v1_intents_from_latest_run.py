from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from args.wa.wa_order_gateway_v1 import enforce_mode_gate


from args.wa.wa_order_gateway_v1 import decide_intent, iter_jsonl, append_jsonl


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

# DEBUG: prove gating without depending on upstream WA actions
# Leave both empty in normal runs.
DEBUG_FORCE_ACTION = ""  # e.g. "ENTER_LONG" / "EXIT" / "REDUCE"
DEBUG_FORCE_KIND = ""    # e.g. "INTENT_ORDER" to simulate an order intent
DEBUG_FORCE_ACTION = "ENTER_LONG"
DEBUG_FORCE_KIND = "INTENT_ORDER"


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


def _read_position_size_runtime() -> int:
    """
    Read runtime position size from args/data/position_state_v0.json (ignored by git).
    """
    p = DATA_DIR / "position_state_v0.json"
    if not p.exists():
        return 0
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            return int(obj.get("size", 0) or 0)
    except Exception:
        return 0
    return 0


def _action_hint_from_wa_action(wa_action: Dict[str, Any], pos_size: int) -> str:
    for k in ("intent", "action", "kind", "signal", "op", "type", "label"):
        v = wa_action.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()

    for k in ("target_position", "target_pos", "target_size"):
        v = wa_action.get(k)
        if isinstance(v, (int, float)):
            tgt = int(v)
            cur = int(pos_size)
            if cur == 0:
                return "ENTER" if tgt != 0 else "HOLD"
            if tgt == 0:
                return "EXIT"
            if abs(tgt) < abs(cur):
                return "REDUCE"
            if abs(tgt) > abs(cur):
                return "ENTER"
            return "HOLD"

    return ""


def _classify_action(action_hint: str) -> str:
    s = (action_hint or "").upper()
    if not s:
        return "UNKNOWN"
    if "ENTER" in s or "OPEN" in s or "NEW" in s:
        return "ENTER"
    if "EXIT" in s or "CLOSE" in s or "REDUCE" in s or "TAKE_PROFIT" in s or "TP" in s:
        return "EXIT"
    return "UNKNOWN"


def _derive_mode_for_row(ma_decision: str, pos_size: int) -> str:
    """
    We derive mode deterministically from row.ma_decision + runtime position.
    This avoids relying on run_report shape (which may not carry mode/position_size).
    """
    d = str(ma_decision or "").strip().upper()
    if d == "ALLOW":
        return "ALLOW_NEW_ENTRIES"
    # any enforced no-trade / unknown / exit => only exits if position exists
    if pos_size != 0:
        return "ONLY_EXITS"
    return "NO_TRADE"


def _gate_intent_kind(kind_raw: str, mode: str, action_class: str) -> Tuple[str, str]:
    k = str(kind_raw or "").strip()

    if k == "INTENT_CANCEL_ALL":
        return k, ""

    if mode == "NO_TRADE":
        if k != "INTENT_NONE":
            return "INTENT_NONE", "mode_NO_TRADE_blocks_all"
        return "INTENT_NONE", ""

    if mode == "ONLY_EXITS":
        if action_class == "ENTER":
            return "INTENT_NONE", "mode_ONLY_EXITS_blocks_ENTER"
        if action_class == "UNKNOWN":
            return "INTENT_NONE", "mode_ONLY_EXITS_blocks_UNKNOWN_ACTION"
        return k, ""

    return k, ""


def main() -> int:
    rp = _latest_report()
    if rp is None:
        print("FAIL: no run_report_*_paper.json found in args/logs")
        return 2

    report = _load_json(rp)
    run_id = str(report.get("run_id") or "")
    if not run_id:
        print(f"FAIL: run_id missing in {rp}")
        return 3

    outputs = report.get("outputs") if isinstance(report.get("outputs"), dict) else {}
    orders_path = outputs.get("orders_paper")
    if not orders_path:
        print(f"FAIL: outputs.orders_paper missing in {rp}")
        return 4

    orders_file = Path(str(orders_path))
    if not orders_file.is_absolute():
        orders_file = REPO_ROOT / orders_file
    if not orders_file.exists():
        print(f"FAIL: orders file not found: {orders_file}")
        return 5

    # runtime position (for ONLY_EXITS)
    pos_size_rt = _read_position_size_runtime()

    out_intents = DATA_DIR / f"orders_intent_{run_id}.jsonl"
    if out_intents.exists():
        out_intents.unlink()

    total = 0
    n_none = 0
    n_order = 0
    n_cancel = 0

    for row in iter_jsonl(orders_file):
        if row.get("kind") != "ORDER_PAPER":
            continue
        total += 1

        wa_action = row.get("wa_action")
        if not isinstance(wa_action, dict):
            wa_action = {}

        ma_decision_row = str(row.get("ma_decision") or "").strip()

        # derive mode from row decision + runtime position
        mode = _derive_mode_for_row(ma_decision_row, pos_size_rt)

        # upstream intent
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
        kind_raw = str(d.get("kind") or intent_obj.kind)

        # DEBUG: force raw kind to prove gating
        if DEBUG_FORCE_KIND:
            kind_raw = str(DEBUG_FORCE_KIND).strip()

        action_hint = _action_hint_from_wa_action(wa_action, pos_size_rt)
        if DEBUG_FORCE_ACTION:
            action_hint = str(DEBUG_FORCE_ACTION).strip().upper()
        action_class = _classify_action(action_hint)

        kind_final, gate_reason = _gate_intent_kind(kind_raw, mode, action_class)

        # write gated record
        d["mode"] = mode
        d["position_size"] = pos_size_rt
        d["kind_raw"] = kind_raw
        d["kind"] = kind_final
        d["action_hint"] = action_hint
        d["action_class"] = action_class
        if gate_reason:
            d["gate_reason"] = gate_reason

        append_jsonl(out_intents, d)

        if kind_final == "INTENT_NONE":
            n_none += 1
        elif kind_final == "INTENT_ORDER":
            n_order += 1
        else:
            n_cancel += 1

    summary = {
        "orders_in_total": total,
        "intent_none": n_none,
        "intent_order": n_order,
        "intent_cancel_all": n_cancel,
        "out_intents": str(out_intents),
        "position_size": pos_size_rt,
        "debug_force_action": DEBUG_FORCE_ACTION,
        "debug_force_kind": DEBUG_FORCE_KIND,
    }

    print("WA_V1_INTENTS")
    print(f"latest_report: {rp}")
    print(f"run_id: {run_id}")
    print(f"orders_in: {orders_file}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

