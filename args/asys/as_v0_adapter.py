
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
def _parse_ts_utc(ts_str: str) -> Optional[datetime]:
    s = (ts_str or "").strip()
    if not s:
        return None
    try:
        # ISO with optional Z
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # treat naive as UTC
            return dt
        # normalize to naive UTC
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _compute_session_flags(ts_utc: Optional[datetime]) -> Dict[str, Any]:
    """
    Minimal, deterministic scaffold for futures session flags (UTC-based).
    This is intentionally conservative and simple for Stage 3.5.
    """
    if ts_utc is None:
        return {
            "is_rth": None,
            "is_globex": None,
            "minutes_to_close": _minutes_to_weekly_close(ts_utc),
            "is_holiday": None,
        }
def _minutes_to_weekly_close(ts_utc: Optional[datetime]) -> Optional[int]:
    """
    Deterministic scaffold:
    - Define weekly close as Friday 22:00 UTC.
    - Return minutes until that close if within the same trading week.
    - If timestamp missing, return None.
    """
    if ts_utc is None:
        return None

    # Friday=4 (Mon=0)
    wd = ts_utc.weekday()
    close_dt = ts_utc.replace(hour=22, minute=0, second=0, microsecond=0)

    # move close_dt to Friday of current week
    delta_days = 4 - wd
    close_dt = close_dt + timedelta(days=delta_days)

    # if already past Friday 22:00 in this week, do not guess next week's schedule here
    if close_dt < ts_utc:
        return 0

    mins = int((close_dt - ts_utc).total_seconds() // 60)
    return max(0, mins)


    wd = ts_utc.weekday()  # Mon=0 ... Sun=6
    # Minimal "holiday/closed" signal:
    # - Saturday: closed
    # - Sunday: closed until approx weekly open (roughly 22:00 UTC)
    is_holiday = False
    if wd == 5:  # Saturday
        is_holiday = True
    if wd == 6 and ts_utc.hour < 22:  # Sunday before rough open
        is_holiday = True

    # Minimal globex open heuristic
    if wd == 5:
        is_globex = False
    elif wd == 6:
        is_globex = ts_utc.hour >= 22
    else:
        is_globex = True

    return {
        "is_rth": None,
        "is_globex": bool(is_globex),
        "minutes_to_close": None,
        "is_holiday": bool(is_holiday),
    }



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
    sess = _compute_session_flags(_parse_ts_utc(bar.ts))
    if isinstance(ma_input.get("session_flags"), dict):
        ma_input["session_flags"].update(sess)


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
