from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ownership reset planner (v0, DRYRUN only)."
    )
    ap.add_argument(
        "--control",
        default="",
        help="control_plane.json (default args/data/control_plane.json)",
    )
    ap.add_argument(
        "--positions",
        default="",
        help="positions snapshot (default args/data/ibkr_positions_live.json)",
    )
    ap.add_argument(
        "--out", default="", help="write plan json (default args/data/reset_plan.json)"
    )
    args = ap.parse_args()

    repo = _repo_root()
    control_path = (
        Path(args.control)
        if args.control
        else (repo / "args" / "data" / "control_plane.json")
    )
    pos_path = (
        Path(args.positions)
        if args.positions
        else (repo / "args" / "data" / "ibkr_positions_live.json")
    )
    out_path = (
        Path(args.out) if args.out else (repo / "args" / "data" / "reset_plan.json")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    control = _read_json(control_path) or {}
    allow = set(control.get("instrument_allowlist") or [])
    global_mode = str(control.get("global_mode", "ALLOW_NEW_ENTRIES"))
    exec_mode = str(control.get("execution_mode", "DRYRUN"))

    pos = _read_json(pos_path) or {}
    rows = pos.get("rows") or []

    actions: List[Dict[str, Any]] = []

    # Policy: in ONLY_EXITS, we plan to FLATTEN everything to get clean baseline
    for r in rows:
        try:
            sym = str(r.get("symbol") or "")
            secType = str(r.get("secType") or "")
            qty = float(r.get("position") or 0.0)
            if abs(qty) <= 0:
                continue
            # Close direction: if long -> SELL qty; if short -> BUY abs(qty)
            side = "SELL" if qty > 0 else "BUY"
            qty_abs = abs(qty)

            owned = (not allow) or (sym in allow)
            action_kind = "CLOSE_ALLOWLIST" if owned else "CLOSE_UNKNOWN"

            actions.append(
                {
                    "kind": action_kind,
                    "symbol": sym,
                    "secType": secType,
                    "qty": qty_abs,
                    "side": side,
                    "priority": 0 if (action_kind == "CLOSE_UNKNOWN") else 1,
                    "notes": "DRYRUN plan only. Requires PAPER execution to send orders.",
                }
            )
        except Exception:
            continue

    actions.sort(key=lambda x: (x.get("priority", 9), x.get("symbol", "")))

    plan = {
        "schema": "reset_plan_v0",
        "ts_utc": _iso_utc_now(),
        "control_plane": str(control_path),
        "global_mode": global_mode,
        "execution_mode": exec_mode,
        "positions_source": str(pos_path),
        "actions": actions,
        "summary": {
            "positions_count": len(
                [r for r in rows if abs(float(r.get("position") or 0.0)) > 0]
            ),
            "actions_count": len(actions),
            "has_unknown": any(a["kind"] == "CLOSE_UNKNOWN" for a in actions),
        },
    }

    out_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
