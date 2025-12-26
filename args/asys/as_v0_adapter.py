
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.contracts.ma_input_contract import ensure_optional_v1

# Repo/data paths (local, deterministic)
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

# Roll window policy (v0.1 scaffold)
ROLL_WINDOW_DAYS = 7

# Simple caches (avoid re-reading JSON per bar)
_EXPIRY_CACHE: Optional[str] = None
_ROLL_FLAGS_CACHE: Optional[Dict[str, Any]] = None


def _read_contract_expiry_yyyymmdd(data_dir: Path = DATA_DIR) -> Optional[str]:
    """
    Read expiry date (YYYYMMDD) from known contract/meta files.
    Priority:
      1) ibkr_hg_contract_v1.json
      2) hg_5m_bars_ibkr.meta.json
    Accepts strings that contain YYYYMMDD anywhere (e.g. "20251229", "20251229 00:00:00").
    """
    candidates = [
        data_dir / "ibkr_hg_contract_v1.json",
        data_dir / "hg_5m_bars_ibkr.meta.json",
    ]

    for p in candidates:
        if not p.exists():
            continue

        try:
            with p.open("r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue

        cand = None
        if isinstance(obj, dict):
            cand = obj.get("lastTradeDateOrContractMonth") or obj.get("expiry") or obj.get("expiry_yyyymmdd")
            if not cand:
                nested = obj.get("contract") or obj.get("contract_identity") or obj.get("resolved_contract")
                if isinstance(nested, dict):
                    cand = nested.get("lastTradeDateOrContractMonth") or nested.get("expiry")

        if cand:
            s = str(cand).strip()
            m = re.search(r"(\d{8})", s)
            if m:
                return m.group(1)

    return None


def _compute_roll_flags(expiry_yyyymmdd: Optional[str]) -> Dict[str, Any]:
    if not expiry_yyyymmdd:
        return {"days_to_expiry": None, "in_roll_window": None}

    try:
        expiry_date = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
        today = datetime.utcnow().date()
        days = int((expiry_date - today).days)
        in_roll = bool(days <= ROLL_WINDOW_DAYS)
        return {"days_to_expiry": days, "in_roll_window": in_roll}
    except Exception:
        return {"days_to_expiry": None, "in_roll_window": None}


def _get_roll_flags_cached() -> Dict[str, Any]:
    global _EXPIRY_CACHE, _ROLL_FLAGS_CACHE
    if _ROLL_FLAGS_CACHE is not None:
        return dict(_ROLL_FLAGS_CACHE)

    if _EXPIRY_CACHE is None:
        _EXPIRY_CACHE = _read_contract_expiry_yyyymmdd()

    _ROLL_FLAGS_CACHE = _compute_roll_flags(_EXPIRY_CACHE)
    return dict(_ROLL_FLAGS_CACHE)


@dataclass(frozen=True)
class Bar5m:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def derive_ma_input_from_bar(
    bar: Bar5m,
    *,
    # Optional knobs for offline scaffolding:
    qc: str = "OK",
    stale_quotes: bool = False,
    missing_bars: int = 0,
    timestamp_drift_ms: int = 0,
    kill_switch: bool = False,
    liquidity: float = 1.0,
    margin_usage: float = 0.0,
    confidence: float = 1.0,
    regime: str = "UNKNOWN",
    tail_risk: str = "UNKNOWN",
    correlation: float = 0.0,
) -> Dict[str, Any]:
    """
    Produce MA input object that matches the contract paths discovered from policy:

    data.missing_bars
    data.qc
    data.stale_quotes
    data.timestamp_drift_ms
    exec.kill_switch
    market.liquidity
    risk.margin_usage
    state.confidence
    state.regime
    state.tail_risk
    stats.correlation
    """
    ma_input: Dict[str, Any] = {
        "data": {
            "missing_bars": int(missing_bars),
            "qc": str(qc),
            "stale_quotes": bool(stale_quotes),
            "timestamp_drift_ms": int(timestamp_drift_ms),
        },
        "exec": {"kill_switch": bool(kill_switch)},
        "market": {"liquidity": _safe_float(liquidity, 1.0)},
        "risk": {"margin_usage": _safe_float(margin_usage, 0.0)},
        "state": {
            "confidence": _safe_float(confidence, 1.0),
            "regime": str(regime),
            "tail_risk": str(tail_risk),
        },
        "stats": {"correlation": _safe_float(correlation, 0.0)},
        # Keep bar data available for future features (not used by MA policy v0)
        "bar": {
            "ts": bar.ts,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        },
    }

    # Ensure optional v1 keys exist (backward compatible)
    ma_input = ensure_optional_v1(ma_input)

    # Populate roll flags (real values)
    roll = _get_roll_flags_cached()
    if isinstance(ma_input.get("roll_flags"), dict):
        ma_input["roll_flags"].update(roll)

    return ma_input


def parse_bar_row(row: Dict[str, str]) -> Bar5m:
    """
    Expected CSV columns:
      ts, open, high, low, close, volume
    """
    return Bar5m(
        ts=str(row.get("ts", "")),
        open=_safe_float(row.get("open", 0.0)),
        high=_safe_float(row.get("high", 0.0)),
        low=_safe_float(row.get("low", 0.0)),
        close=_safe_float(row.get("close", 0.0)),
        volume=_safe_float(row.get("volume", 0.0)),
    )


def pick_latest_bar(bars: List[Bar5m]) -> Optional[Bar5m]:
    if not bars:
        return None
    # For v0: assume input order already chronological; pick last.
    return bars[-1]
