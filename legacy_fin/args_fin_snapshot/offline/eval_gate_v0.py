from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from args.offline.evidence_history_v0 import record_json_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "args" / "offline" / "models"


def _utc_now_iso() -> str:
    dt = datetime.now(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


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
    cands = [
        p
        for p in MODELS_DIR.iterdir()
        if p.is_dir() and (p / "model_manifest.json").exists()
    ]
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
        return False, {
            "reason": "NO_METRICS",
            "checks": {},
            "fails": {"metrics": "NO_METRICS"},
            "warnings": {},
        }

    rows = _as_int(metrics.get("rows"), 0)
    payload_kind = (
        metrics.get("payload_kind")
        if isinstance(metrics.get("payload_kind"), dict)
        else {}
    )
    ma_decision = (
        metrics.get("ma_decision")
        if isinstance(metrics.get("ma_decision"), dict)
        else {}
    )
    missing = (
        metrics.get("missing_ratio_top10")
        if isinstance(metrics.get("missing_ratio_top10"), dict)
        else {}
    )

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
        "min_rows": int(min_rows),
        "payload_order_share": float(share_po),
        "require_payload_order_share_gte": float(require_payload_order_share_gte),
        "no_trade_share": float(share_nt),
        "allow_all_no_trade": bool(allow_all_no_trade),
        "missing_reason": float(miss_reason),
        "max_missing_reason": float(max_missing_reason),
        "missing_gate_reason": float(miss_gate),
        "max_missing_gate_reason": float(max_missing_gate_reason),
    }

    ok = True
    fails: Dict[str, str] = {}
    warnings: Dict[str, str] = {}

    if rows < min_rows:
        ok = False
        fails["rows"] = "ROWS_BELOW_MIN"

    if share_po < require_payload_order_share_gte:
        ok = False
        fails["payload_order_share"] = "PAYLOAD_ORDER_SHARE_TOO_LOW"

    # ALL_NO_TRADE handling:
    # - if not allowed -> FAIL
    # - if allowed -> PASS (unless other checks fail) + warning
    if share_nt >= 0.999:
        if allow_all_no_trade:
            warnings["no_trade_share"] = "ALL_NO_TRADE_ALLOWED"
        else:
            ok = False
            fails["no_trade_share"] = "ALL_NO_TRADE_NOT_ALLOWED"

    if miss_reason > max_missing_reason:
        ok = False
        fails["missing_reason"] = "MISSING_REASON_TOO_HIGH"

    if miss_gate > max_missing_gate_reason:
        ok = False
        fails["missing_gate_reason"] = "MISSING_GATE_REASON_TOO_HIGH"

    return ok, {"checks": checks, "fails": fails, "warnings": warnings}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("eval_gate_v0")

    ap.add_argument(
        "--model",
        default="",
        help="Model dir name under args/offline/models (default: latest)",
    )
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--payload-order-share-gte", type=float, default=0.0)
    ap.add_argument(
        "--allow-all-no-trade",
        action="store_true",
        help="Allow dataset where all decisions are NO_TRADE (adds warning)",
    )
    ap.add_argument("--max-missing-reason", type=float, default=1.0)
    ap.add_argument("--max-missing-gate-reason", type=float, default=1.0)

    # Stage 8.5 evidence history
    ap.add_argument(
        "--evidence-dir",
        default="args/offline/evidence/eval_gate",
        help="Evidence history dir (timestamped + latest.json)",
    )
    ap.add_argument(
        "--no-evidence",
        action="store_true",
        help="Disable evidence history writes (not recommended)",
    )

    args = ap.parse_args(argv)
    generated_at_utc = _utc_now_iso()

    ok: bool = False
    model_dir: Optional[Path] = None

    try:
        # Resolve model dir
        model_dir = (
            (MODELS_DIR / args.model.strip())
            if args.model.strip()
            else _latest_model_dir()
        )

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

        report: Dict[str, Any] = {
            "schema": "eval_gate_v0",
            "generated_at_utc": generated_at_utc,
            "status": "PASS" if ok else "FAIL",
            "ok": bool(ok),
            "model_dir": str(model_dir),
            "model_id": man.get("model_id"),
            "manifest": str(mf_path),
            **details,
        }

    except Exception as e:
        report = {
            "schema": "eval_gate_v0",
            "generated_at_utc": generated_at_utc,
            "status": "FAIL",
            "ok": False,
            "error": {"type": type(e).__name__, "message": str(e)},
        }
        ok = False
        model_dir = None

    # Write primary report (first pass)
    if isinstance(model_dir, Path):
        out_path = model_dir / "eval_gate_report.json"
        report["report_path"] = str(out_path)
        _write_json(out_path, report)

    # Evidence history (timestamped copy + latest pointer)
    # IMPORTANT: we then rewrite the primary report with evidence_latest included
    if (not args.no_evidence) and isinstance(model_dir, Path):
        try:
            ptr = record_json_evidence(
                kind="eval_gate",
                report_obj=report,
                evidence_dir=Path(args.evidence_dir),
                report_basename="eval_gate_report.json",
            )
            report["evidence_latest"] = ptr

            # rewrite primary report so it includes evidence pointer
            out_path2 = Path(report["report_path"])
            _write_json(out_path2, report)

        except Exception as e:
            # Evidence failure is a hard FAIL (audit must be reliable)
            report["status"] = "FAIL"
            report["ok"] = False
            report["error_evidence"] = {"type": type(e).__name__, "message": str(e)}
            ok = False

            if isinstance(model_dir, Path) and "report_path" in report:
                _write_json(Path(report["report_path"]), report)

    # stdout: JSON only
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
