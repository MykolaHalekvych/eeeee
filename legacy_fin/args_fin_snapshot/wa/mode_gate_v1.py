from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Stage A: semantic intent kinds
KIND_NONE = "INTENT_NONE"
KIND_CANCEL_ALL = "INTENT_CANCEL_ALL"
KIND_ENTRY = "INTENT_ENTRY"
KIND_EXIT = "INTENT_EXIT"
KIND_REDUCE = "INTENT_REDUCE"
KIND_TAKE_PROFIT = "INTENT_TAKE_PROFIT"

_ALLOWED_KINDS = {
    KIND_NONE,
    KIND_CANCEL_ALL,
    KIND_ENTRY,
    KIND_EXIT,
    KIND_REDUCE,
    KIND_TAKE_PROFIT,
}

MODE_ALLOW = "ALLOW_NEW_ENTRIES"
MODE_ONLY_EXITS = "ONLY_EXITS"
MODE_NO_TRADE = "NO_TRADE"
_ALLOWED_MODES = {MODE_ALLOW, MODE_ONLY_EXITS, MODE_NO_TRADE}


def _u(x: Any) -> str:
    return str(x).strip().upper().replace("-", "_") if x is not None else ""


def extract_mode(run_report: Dict[str, Any]) -> str:
    # common shapes:
    # report["risk_envelope"]["mode"]
    # report["ma_report"]["risk_envelope"]["mode"]
    m = None
    re = run_report.get("risk_envelope")
    if isinstance(re, dict):
        m = re.get("mode")

    if m is None:
        ma = run_report.get("ma_report")
        if isinstance(ma, dict):
            re2 = ma.get("risk_envelope")
            if isinstance(re2, dict):
                m = re2.get("mode")

    mu = _u(m)
    if mu in _ALLOWED_MODES:
        return mu

    mu2 = _u(run_report.get("risk_envelope_mode"))
    if mu2 in _ALLOWED_MODES:
        return mu2

    # fail-safe
    return MODE_NO_TRADE


def extract_position_size(run_report: Dict[str, Any]) -> float:
    def _try(d: Any) -> Optional[float]:
        if not isinstance(d, dict):
            return None
        v = d.get("size")
        if isinstance(v, (int, float)):
            return float(v)
        v2 = d.get("position_size")
        if isinstance(v2, (int, float)):
            return float(v2)
        return None

    v = _try(run_report.get("position_state"))
    if v is not None:
        return v

    ma = run_report.get("ma_report")
    if isinstance(ma, dict):
        v = _try(ma.get("position_state"))
        if v is not None:
            return v

    v3 = run_report.get("position_size")
    if isinstance(v3, (int, float)):
        return float(v3)

    # Stage A: unknown position treated as 0.0 (fail-safe on entries handled by mode)
    return 0.0


def _extract_kind(intent: Dict[str, Any]) -> Tuple[str, str]:
    """
    Returns (kind, kind_raw) where:
    - kind is semantic kind (normalized)
    - kind_raw is original marker for audit
    """
    raw = intent.get("kind_raw")
    if not isinstance(raw, str) or not raw.strip():
        # fallbacks
        for key in ("kind", "intent_kind", "intent", "action", "signal"):
            v = intent.get(key)
            if isinstance(v, str) and v.strip():
                raw = v
                break
        else:
            raw = ""

    kind = _u(intent.get("kind"))
    kind_raw = _u(raw)

    # If kind missing or unknown, try using kind_raw
    if kind not in _ALLOWED_KINDS:
        kind = kind_raw if kind_raw in _ALLOWED_KINDS else KIND_NONE

    return kind, raw if isinstance(raw, str) else ""


def gate_kind(kind: str, mode: str) -> Tuple[str, Optional[str]]:
    k = _u(kind)
    m = _u(mode)

    if m not in _ALLOWED_MODES:
        return KIND_NONE, f"mode_unknown:{m or 'EMPTY'}"

    # Always allow emergency cancel-all intent through the gate.
    if k == KIND_CANCEL_ALL:
        return KIND_CANCEL_ALL, None

    # Exits are always allowed (core canon) even under NO_TRADE.
    if k in {KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}:
        return k, None

    if m == MODE_NO_TRADE:
        # NO_TRADE blocks new entries; leave NONE as NONE.
        if k == KIND_NONE:
            return KIND_NONE, "mode_no_trade"
        return KIND_NONE, f"mode_no_trade_blocks:{k}"

    if m == MODE_ONLY_EXITS:
        # Only exits allowed; entries blocked.
        if k == KIND_NONE:
            return KIND_NONE, "only_exits_none"
        return KIND_NONE, f"only_exits_blocks:{k}"

    # MODE_ALLOW_NEW_ENTRIES
    if k == KIND_NONE:
        return KIND_NONE, "allow_none"
    if k == KIND_ENTRY:
        return KIND_ENTRY, None

    # Unknown kinds are treated as NONE for safety
    return KIND_NONE, f"unknown_kind:{k}"


def apply_mode_gate_to_intent(
    intent: Dict[str, Any], mode: str, position_size: float
) -> Dict[str, Any]:
    if not isinstance(intent, dict):
        return {
            "kind": KIND_NONE,
            "kind_raw": str(intent),
            "gate_reason": "intent_not_dict",
            "mode": MODE_NO_TRADE,
            "position_size": 0.0,
        }

    kind, raw = _extract_kind(intent)

    intent["mode"] = mode
    intent["position_size"] = position_size
    intent["kind_raw"] = raw

    gated_kind, reason = gate_kind(kind, mode)
    intent["kind"] = gated_kind

    if reason:
        intent["gate_reason"] = reason
    else:
        intent.pop("gate_reason", None)

    return intent


def apply_mode_gate_from_report(
    intent: Dict[str, Any], run_report: Dict[str, Any]
) -> Dict[str, Any]:
    if not isinstance(run_report, dict):
        run_report = {}
    mode = extract_mode(run_report)
    pos = extract_position_size(run_report)
    return apply_mode_gate_to_intent(intent, mode, pos)
