# args/ops/reset_verify_v0.py
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.ibkr.ibkr_positions_snapshotter_v0 import snapshot_positions

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

SCHEMA = "reset_verify_v0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _normalize_allowlist(cp: Dict[str, Any]) -> List[str]:
    # Back-compat: accept multiple historical keys
    raw = (
        cp.get("allowlist")
        or cp.get("allowlist_symbols")
        or cp.get("instrument_allowlist")
        or cp.get("instrumentAllowlist")
        or []
    )
    if isinstance(raw, str):
        lst = [raw]
    else:
        try:
            lst = list(raw)
        except Exception:
            lst = []

    seen = set()
    out: List[str] = []
    for s in lst:
        k = _u(s)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _is_allowlisted(symbol: str, local_symbol: str, allowlist_norm: List[str]) -> bool:
    s = _u(symbol)
    l = _u(local_symbol)
    for a in allowlist_norm:
        if not a:
            continue
        # Exact match on symbol
        if s == a:
            return True
        # Futures localSymbol often starts with base symbol (e.g., MHGG6 startswith MHG)
        if l == a or l.startswith(a):
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)
    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--timeout-s", type=float, default=25.0)
    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--out", default=str(DATA_DIR / "reset_verify_report.json"))
    args = ap.parse_args(argv)

    ts = _utc_now_iso()

    # Load control plane + allowlist
    try:
        cp = _read_json(Path(args.control_plane))
    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"control_plane_read_failed: {type(e).__name__}: {e}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    allowlist_norm = _normalize_allowlist(cp)

    # Snapshot positions
    snap = snapshot_positions(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        connect_timeout_s=float(args.connect_timeout_s),
        timeout_s=float(args.timeout_s),
    )

    if not snap.get("ok"):
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "error": f"positions_snapshot_failed: {snap.get('error')}",
        }
        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2

    unknown_nonzero: List[Dict[str, Any]] = []
    allow_nonzero: List[Dict[str, Any]] = []
    total_nonzero = 0

    for r in snap.get("rows", []):
        pos = float(r.get("position") or 0.0)
        if abs(pos) < 1e-9:
            continue
        total_nonzero += 1

        sym = str(r.get("symbol") or "")
        ls = str(r.get("localSymbol") or "")
        sec = str(r.get("secType") or "")

        entry = {
            "symbol": sym,
            "localSymbol": ls,
            "secType": sec,
            "conId": int(r.get("conId") or 0),
            "position": pos,
        }

        if _is_allowlisted(sym, ls, allowlist_norm):
            allow_nonzero.append(entry)
        else:
            unknown_nonzero.append(entry)

    baseline_flat = (total_nonzero == 0)

    warnings: List[str] = []
    if not allowlist_norm:
        warnings.append("allowlist_empty: all positions classified as UNKNOWN")

    out = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": True,
        "exit_code": 0 if baseline_flat else 1,
        "allowlist": allowlist_norm,
        "baseline_flat": baseline_flat,
        "counts": {
            "nonzero_total": total_nonzero,
            "nonzero_unknown": len(unknown_nonzero),
            "nonzero_allowlist": len(allow_nonzero),
        },
        "nonzero_unknown": unknown_nonzero[:25],
        "nonzero_allowlist": allow_nonzero[:25],
        "warnings": warnings,
        "notes": [
            "exit_code=1 means NOT FLAT (expected before executing reset).",
            "After real reset execution, baseline_flat must become true.",
        ],
    }

    _write_json(Path(args.out), out)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return int(out["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
