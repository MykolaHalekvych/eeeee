from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "args" / "offline" / "models"


def _read_json_first_obj(path: Path) -> Dict[str, Any]:
    """
    Tolerant JSON reader: parses first JSON object only (ignores trailing data).
    """
    s = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not s:
        raise ValueError(f"{path} empty")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(s)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} first JSON is not object")
    return obj


def _latest_model_dir() -> Path:
    if not MODELS_DIR.exists():
        raise FileNotFoundError(f"models dir missing: {MODELS_DIR}")
    cands = [p for p in MODELS_DIR.iterdir() if p.is_dir() and (p / "model_manifest.json").exists()]
    if not cands:
        raise FileNotFoundError("no model_manifest.json under args/offline/models")
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _as_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _as_float(x: Any, default: float) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _get_metric(d: Dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def eval_gate(
    manifest: Dict[str, Any],
    *,
    min_rows: int,
    require_payload_order_share_gte: float,
    max_missing_reason: float,
    max_missing_gate_reason: float,
    allow_all_no_trade: bool,
) -> Tuple[bool, Dict[str, Any]]:
    metrics = manifest.get("metrics")
    if not isinstance(metrics, dict):
        return False, {"reason": "NO_METRICS"}

    rows = _as_int(metrics.get("rows"), 0)
    payload_kind = metrics.get("payload_kind") if isinstance(metrics.get("payload_kind"), dict) else {}
    ma_decision = metrics.get("ma_decision") if isinstance(metrics.get("ma_decision"), dict) else {}
    missing = metrics.get("missing_ratio_top10") if isinstance(metrics.get("missing_ratio_top10"), dict) else {}

    # payload order share
    po = _as_int(payload_kind.get("PAYLOAD_ORDER"), 0)
    share_po = (po / rows) if rows > 0 else 0.0

    # NO_TRADE share
    nt = _as_int(ma_decision.get("NO_TRADE"), 0)
    share_nt = (nt / rows) if rows > 0 else 0.0

    miss_reason = _as_float(missing.get("reason"), 0.0)
    miss_gate = _as_float(missing.get("gate_reason"), 0.0)

    checks: Dict[str, Any] = {
        "rows": rows,
        "min_rows": min_rows,
        "payload_order_share": share_po,
        "require_payload_order_share_gte": require_payload_order_share_gte,
        "no_trade_share": share_nt,
        "allow_all_no_trade": bool(allow_all_no_trade),
        "missing_reason": miss_reason,
        "max_missing_reason": max_missing_reason,
        "missing_gate_reason": miss_gate,
        "max_missing_gate_reason": max_missing_gate_reason,
    }

    # core checks
    ok = True
    fails: Dict[str, str] = {}

    if rows < min_rows:
        ok = False
        fails["rows"] = "ROWS_BELOW_MIN"

    if share_po < require_payload_order_share_gte:
        ok = False
        fails["payload_order_share"] = "PAYLOAD_ORDER_SHARE_TOO_LOW"

    # if all NO_TRADE and not allowed -> fail
    if (share_nt >= 0.999) and (not allow_all_no_trade):
        ok = False
        fails["no_trade_share"] = "ALL_NO_TRADE_NOT_ALLOWED"

    # missing caps (MVP: reason/gate_reason may be missing in v0; keep caps configurable)
    if miss_reason > max_missing_reason:
        ok = False
        fails["missing_reason"] = "MISSING_REASON_TOO_HIGH"

    if miss_gate > max_missing_gate_reason:
        ok = False
        fails["missing_gate_reason"] = "MISSING_GATE_REASON_TOO_HIGH"

    out = {"checks": checks, "fails": fails}
    return ok, out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("eval_gate_v0")
    ap.add_argument("--model", default="", help="Model dir name under args/offline/models (default: latest)")
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--payload-order-share-gte", type=float, default=0.0)
    ap.add_argument("--allow-all-no-trade", action="store_true", help="Allow dataset where all decisions are NO_TRADE")
    ap.add_argument("--max-missing-reason", type=float, default=1.0)
    ap.add_argument("--max-missing-gate-reason", type=float, default=1.0)
    args = ap.parse_args(argv)

    if args.model.strip():
        model_dir = MODELS_DIR / args.model.strip()
    else:
        model_dir = _latest_model_dir()

    mf_path = model_dir / "model_manifest.json"
    man = _read_json_first_obj(mf_path)

    ok, details = eval_gate(
        man,
        min_rows=max(1, int(args.min_rows)),
        require_payload_order_share_gte=float(args.payload_order_share_gte),
        max_missing_reason=float(args.max_missing_reason),
        max_missing_gate_reason=float(args.max_missing_gate_reason),
        allow_all_no_trade=bool(args.allow_all_no_trade),
    )

    report = {
        "schema": "eval_gate_v0",
        "ok": bool(ok),
        "model_dir": str(model_dir),
        "model_id": man.get("model_id"),
        "manifest": str(mf_path),
        **details,
    }

    out_path = model_dir / "eval_gate_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("EVAL_GATE_V0")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
