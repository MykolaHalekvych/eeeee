from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from args.ibkr.ibkr_order_payload_v1 import build_contract_ref, intent_to_payload
from args.wa.order_intents_v1 import build_order_intents
from args.wa.wa_action_schema_v1 import apply_wa_action_schema_v1
from args.wa.wa_order_gateway_v1 import (
    append_jsonl,
    decide_intent,
    enforce_mode_gate,
    iter_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


# -----------------------------
# Helpers
# -----------------------------
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
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object")
    return obj


def _resolve_path(p: Any) -> Path:
    pp = Path(str(p))
    return pp if pp.is_absolute() else (REPO_ROOT / pp)


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, int):
        return bool(x)
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"true", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "0", "no", "n", "off"}:
            return False
    return default


# -----------------------------
# Contract meta
# -----------------------------
def _load_contract_meta_from_report(report: Dict[str, Any]) -> Dict[str, Any]:
    inp = report.get("inputs")
    if not isinstance(inp, dict):
        raise ValueError("run_report.inputs missing")

    csv_meta = inp.get("csv_meta")
    if isinstance(csv_meta, dict) and isinstance(csv_meta.get("contract"), dict):
        return csv_meta["contract"]

    meta_path = inp.get("csv_meta_path")
    if meta_path:
        mp = _resolve_path(meta_path)
        if mp.exists():
            obj = _load_json(mp)
            c = obj.get("contract")
            if isinstance(c, dict):
                return c

    raise ValueError("No contract meta found in run_report (csv_meta / csv_meta_path)")


# -----------------------------
# Control plane (Level 5.2)
# -----------------------------
def _load_control_state() -> Optional[Dict[str, Any]]:
    p = DATA_DIR / "control_state.json"
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


@dataclass(frozen=True)
class WaTestConfig:
    enabled: bool
    force_intent_order: bool
    allow_order_payloads: bool
    bypass_mode_gate: bool
    test_order: Dict[str, Any]


def _wa_test_config(control_state: Optional[Dict[str, Any]]) -> WaTestConfig:
    """
    Expected control_state snippet:

    "wa_test": {
      "enabled": true,
      "force_intent_order": true,
      "allow_order_payloads": true,
      "bypass_mode_gate": true,
      "test_order": {
        "action": "BUY",
        "orderType": "LMT",
        "qty": 1,
        "lmtPrice": 0.01,
        "tif": "DAY"
      }
    }

    Safe-by-default: disabled unless dict + enabled==true.
    """
    if not isinstance(control_state, dict):
        return WaTestConfig(False, False, False, False, {})

    wt = control_state.get("wa_test")
    if not isinstance(wt, dict):
        return WaTestConfig(False, False, False, False, {})

    enabled = _as_bool(wt.get("enabled"), False)
    force_intent_order = _as_bool(wt.get("force_intent_order"), enabled)
    allow_order_payloads = _as_bool(wt.get("allow_order_payloads"), enabled)
    bypass_mode_gate = _as_bool(wt.get("bypass_mode_gate"), False)

    to = wt.get("test_order")
    test_order = to if isinstance(to, dict) else {}

    return WaTestConfig(
        enabled=enabled,
        force_intent_order=force_intent_order,
        allow_order_payloads=allow_order_payloads,
        bypass_mode_gate=bypass_mode_gate,
        test_order=test_order,
    )


def _stage5_max_lmt_price(
    control_state: Optional[Dict[str, Any]], default: float = 0.05
) -> float:
    if not isinstance(control_state, dict):
        return default
    try:
        v = float(control_state.get("stage5_test_max_lmt_price", default))
        return v if v > 0 else default
    except Exception:
        return default


def _seed_test_wa_action(
    *,
    run_id: str,
    index: int,
    control_state: Optional[Dict[str, Any]],
    wt: WaTestConfig,
) -> Dict[str, Any]:
    """
    DEMO-ONLY: minimal deterministic order instruction.
    Keep it Stage5-friendly: BUY LMT qty=1 lmtPrice <= stage5_test_max_lmt_price.
    """
    max_lmt = _stage5_max_lmt_price(control_state, default=0.05)

    action = str(wt.test_order.get("action") or "BUY").strip().upper()
    order_type = str(wt.test_order.get("orderType") or "LMT").strip().upper()
    tif = str(wt.test_order.get("tif") or "DAY").strip().upper()

    try:
        qty = float(wt.test_order.get("qty", 1))
    except Exception:
        qty = 1.0
    if qty <= 0:
        qty = 1.0

    try:
        lmt = float(wt.test_order.get("lmtPrice", min(0.01, max_lmt)))
    except Exception:
        lmt = min(0.01, max_lmt)

    # enforce cap
    if lmt > max_lmt:
        lmt = max_lmt

    # Provide both "action" and "side" to satisfy different translators (defensive).
    return {
        # WA-level command (what intent_to_payload typically keys off)
        "action": "PLACE_ORDER",
        # Order spec (what sender later needs)
        "order": {
            "action": action,  # BUY/SELL
            "orderType": order_type,  # LMT
            "totalQuantity": qty,  # 1
            "lmtPrice": lmt,  # <= stage5 cap
            "tif": tif,  # DAY
            "transmit": False,  # sendplan expects False
        },
        # Idempotency key must match Stage5 allowlist prefix
        "idempotency_key": f"STAGE5_TEST_LMT_{run_id}_{index:06d}",
    }


# -----------------------------
# Intent artifacts
# -----------------------------
def _ensure_raw_intents(report: Dict[str, Any]) -> Tuple[str, Path]:
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id missing in report")

    intents_path = DATA_DIR / f"orders_intent_{run_id}.jsonl"
    if intents_path.exists():
        return run_id, intents_path

    out = report.get("outputs")
    if not isinstance(out, dict):
        raise ValueError("report.outputs missing")
    orders_paper = out.get("orders_paper")
    if not orders_paper:
        raise ValueError("report.outputs.orders_paper missing")
    orders_file = _resolve_path(orders_paper)
    if not orders_file.exists():
        raise ValueError(f"orders_paper file missing: {orders_file}")

    hsum = report.get("harness_summary")
    halted = False
    halt_reason = ""
    if isinstance(hsum, dict):
        halted = (
            bool(hsum.get("halted"))
            if isinstance(hsum.get("halted"), (bool, int))
            else False
        )
        halt_reason = str(hsum.get("halt_reason") or "")

    if intents_path.exists():
        intents_path.unlink()

    if halted:
        intent_obj = decide_intent(
            run_id=run_id,
            index=None,
            ts=None,
            instrument=str(report.get("instrument") or "HG"),
            timeframe=str(report.get("timeframe") or "5m"),
            ma_decision="UNKNOWN",
            wa_action={},
            halted=True,
            halt_reason=halt_reason,
        )
        payload["wa_action_debug"] = gated_intent.get("wa_action")
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
            instrument=str(row.get("instrument") or report.get("instrument") or "HG"),
            timeframe=str(row.get("timeframe") or report.get("timeframe") or "5m"),
            ma_decision=row.get("ma_decision"),
            wa_action=wa_action,
            halted=False,
            halt_reason="",
        )
        append_jsonl(intents_path, intent_obj.to_dict())

    return run_id, intents_path


def _ensure_order_intents(report: Dict[str, Any]) -> Tuple[str, Path, Dict[str, Any]]:
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id missing in report")

    order_intents_path = DATA_DIR / f"order_intents_{run_id}.jsonl"
    if order_intents_path.exists():
        return run_id, order_intents_path, {"note": "exists"}

    _, raw_intents_path = _ensure_raw_intents(report)
    oi_summary = build_order_intents(
        report, raw_intents_path, order_intents_path, source="wa_v1"
    )
    return run_id, order_intents_path, oi_summary


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    rp = _latest_report()
    if rp is None:
        print("FAIL: no run_report_*_paper.json found in args/logs")
        return 2

    report = _load_json(rp)

    run_id, raw_intents_path = _ensure_raw_intents(report)
    _, order_intents_path, oi_summary = _ensure_order_intents(report)

    contract_meta = _load_contract_meta_from_report(report)
    contract_ref = build_contract_ref(contract_meta)

    control_state = _load_control_state()
    control_state_loaded = isinstance(control_state, dict)
    wt = _wa_test_config(control_state)

    wa_test_on = bool(wt.enabled)
    allow_order_payloads = bool(wt.allow_order_payloads)

    out_payload = DATA_DIR / f"orders_payload_{run_id}.jsonl"
    if out_payload.exists():
        out_payload.unlink()

    n_total = 0
    n_none = 0
    n_order = 0
    n_cancel = 0
    n_gated = 0
    n_schema_applied = 0
    n_forced_intent_order = 0
    n_blocked_no_control = 0
    n_bypassed_mode_gate = 0
    n_seeded_test_action = 0

    for rec in iter_jsonl(raw_intents_path):
        idx = n_total
        n_total += 1

        # DEMO: optionally force INTENT_ORDER
        if wa_test_on and wt.force_intent_order:
            rr = dict(rec)
            rr["kind_raw"] = rr.get("kind_raw") or rr.get("kind") or "INTENT_ORDER"
            rr["kind"] = "INTENT_ORDER"
            rec = rr
            n_forced_intent_order += 1

        # Mode gate (optionally bypass in wa_test for INTENT_ORDER)
        if (
            wa_test_on
            and wt.bypass_mode_gate
            and str(rec.get("kind") or "").strip().upper() == "INTENT_ORDER"
        ):
            gated_intent = dict(rec)
            gated_intent["gate_reason"] = (
                gated_intent.get("gate_reason") or "wa_test_bypass_mode_gate"
            )
            n_bypassed_mode_gate += 1
        else:
            gated_intent = enforce_mode_gate(rec, report)

        if gated_intent.get("kind") == "INTENT_NONE" and gated_intent.get(
            "gate_reason"
        ):
            n_gated += 1

        # Apply WA Action Schema v1
        mode = str(gated_intent.get("mode") or "")
        pos = _as_float(gated_intent.get("position_size"), 0.0)
        gated_intent = apply_wa_action_schema_v1(
            gated_intent,
            mode=mode,
            position_size=pos,
            control_state=control_state,
        )
        n_schema_applied += 1

        # DEMO: seed minimal wa_action for INTENT_ORDER if missing/empty
        if (
            wa_test_on
            and str(gated_intent.get("kind") or "").strip().upper() == "INTENT_ORDER"
        ):
            wa_action = gated_intent.get("wa_action")
            if not isinstance(wa_action, dict) or not wa_action:
                gated_intent = dict(gated_intent)
                gated_intent["wa_action"] = _seed_test_wa_action(
                    run_id=run_id, index=idx, control_state=control_state, wt=wt
                )
                gated_intent["wa_action_reason"] = "WA_TEST_SEED_ORDER"
                gated_intent["wa_action_schema"] = (
                    gated_intent.get("wa_action_schema") or "wa_action_schema_v1"
                )
                n_seeded_test_action += 1

        # HARD SAFE DEFAULT: no order payloads unless explicitly allowed
        if (not allow_order_payloads) and str(
            gated_intent.get("kind") or ""
        ).strip().upper() == "INTENT_ORDER":
            n_blocked_no_control += 1
            gated_intent = dict(gated_intent)
            gated_intent["kind_raw"] = (
                gated_intent.get("kind_raw")
                or gated_intent.get("kind")
                or "INTENT_ORDER"
            )
            gated_intent["kind"] = "INTENT_NONE"
            gated_intent["wa_action"] = {}
            gated_intent["wa_action_reason"] = "SAFE_DEFAULT_NO_ALLOW_ORDER_PAYLOADS"
            gated_intent["wa_action_schema"] = (
                gated_intent.get("wa_action_schema") or "wa_action_schema_v1"
            )

        payload = intent_to_payload(gated_intent, contract_ref, dry_run=True)

        # Audit fields
        payload["mode"] = gated_intent.get("mode")
        payload["position_size"] = gated_intent.get("position_size")
        payload["intent_kind_raw"] = gated_intent.get("kind_raw")
        payload["intent_kind"] = gated_intent.get("kind")
        if gated_intent.get("gate_reason"):
            payload["gate_reason"] = gated_intent.get("gate_reason")
        payload["wa_action_schema"] = gated_intent.get("wa_action_schema")
        payload["wa_action_reason"] = gated_intent.get("wa_action_reason")

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
        "raw_intents_in": str(raw_intents_path),
        "order_intents_contract": str(order_intents_path),
        "order_intents_summary": oi_summary,
        "payload_out": str(out_payload),
        "payload_total": n_total,
        "payload_none": n_none,
        "payload_order": n_order,
        "payload_cancel_all": n_cancel,
        "gated_total": n_gated,
        "wa_schema_applied": n_schema_applied,
        "control_state_path": str(DATA_DIR / "control_state.json"),
        "control_state_loaded": bool(control_state_loaded),
        "wa_test_enabled": bool(wa_test_on),
        "wa_test_cfg": {
            "enabled": wt.enabled,
            "force_intent_order": wt.force_intent_order,
            "allow_order_payloads": wt.allow_order_payloads,
            "bypass_mode_gate": wt.bypass_mode_gate,
            "test_order": wt.test_order,
        },
        "allow_order_payloads": bool(allow_order_payloads),
        "forced_intent_order": n_forced_intent_order,
        "bypassed_mode_gate": n_bypassed_mode_gate,
        "seeded_test_action": n_seeded_test_action,
        "blocked_intent_order_no_control": n_blocked_no_control,
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
