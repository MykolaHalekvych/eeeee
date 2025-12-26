from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

KIND_NONE = "INTENT_NONE"

MODE_ALLOW = "ALLOW_NEW_ENTRIES"
MODE_ONLY_EXITS = "ONLY_EXITS"
MODE_NO_TRADE = "NO_TRADE"
_ALLOWED_MODES = {MODE_ALLOW, MODE_ONLY_EXITS, MODE_NO_TRADE}


def _u(x: Any) -> str:
    return str(x).strip().upper() if x is not None else ""


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

    return 0.0


def _is_exit_kind(kind_u: str) -> bool:
    # Allow EXIT/REDUCE/TAKE_PROFIT (and common variants)
    if kind_u in {"EXIT", "REDUCE", "TAKE_PROFIT", "TP", "CLOSE"}:
        return True
    if kind_u.startswith("EXIT") or kind_u.startswith("REDUCE") or kind_u.startswith("TAKE_PROFIT"):
        return True
    if "EXIT" in kind_u or "REDUCE" in kind_u or "TAKE_PROFIT" in kind_u:
        return True
    return False


def gate_kind(kind_raw: Any, mode: Any) -> Tuple[str, Optional[str]]:
    k_raw = str(kind_raw) if kind_raw is not None else ""
    k_u = _u(k_raw)
    m_u = _u(mode)

    if m_u not in _ALLOWED_MODES:
        return KIND_NONE, f"mode_unknown:{m_u or 'EMPTY'}"

    if m_u == MODE_NO_TRADE:
        if k_u in {"", "NONE", KIND_NONE}:
            return KIND_NONE, "mode_no_trade"
        return KIND_NONE, f"mode_no_trade_blocks:{k_u}"

    if m_u == MODE_ONLY_EXITS:
        if k_u in {"", "NONE", KIND_NONE}:
            return KIND_NONE, "only_exits_empty"
        if _is_exit_kind(k_u):
            return k_u, None
        return KIND_NONE, f"only_exits_blocks:{k_u}"

    # MODE_ALLOW
    if k_u in {"", "NONE"}:
        return KIND_NONE, "allow_empty"
    return k_u, None


def apply_mode_gate_to_intent(intent: Dict[str, Any], mode: str, position_size: float) -> Dict[str, Any]:
    # idempotent
    raw = intent.get("kind_raw")
    if not isinstance(raw, str) or not raw.strip():
        # fallbacks from common fields
        for key in ("kind", "intent", "action", "signal"):
            v = intent.get(key)
            if isinstance(v, str) and v.strip():
                raw = v
                break
        else:
            raw = ""

    intent["mode"] = mode
    intent["position_size"] = position_size
    intent["kind_raw"] = raw

    kind, reason = gate_kind(raw, mode)
    intent["kind"] = kind

    if reason:
        intent["gate_reason"] = reason
    else:
        intent.pop("gate_reason", None)

    return intent


def apply_mode_gate_from_report(intent: Dict[str, Any], run_report: Dict[str, Any]) -> Dict[str, Any]:
    mode = extract_mode(run_report)
    pos = extract_position_size(run_report)
    return apply_mode_gate_to_intent(intent, mode, pos)
