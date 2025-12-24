from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


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
    return {
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
