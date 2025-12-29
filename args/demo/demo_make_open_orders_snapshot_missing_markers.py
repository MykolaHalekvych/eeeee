from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


SNAPSHOT_SCHEMA_VERSION = "ibkr_open_orders_snapshot_v0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_jsonl(path: Path, rows: list[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(prog="demo_make_open_orders_snapshot_missing_markers")
    ap.add_argument("--out", default="args/data/ibkr_open_orders_missing_markers.jsonl")
    args = ap.parse_args()

    repo = _repo_root()
    out = Path(str(args.out))
    if not out.is_absolute():
        out = repo / out

    ts = _iso_utc_now()

    # Intentionally omit IBKR_SNAPSHOT_START/END markers.
    # Add a single open-order row (so "empty" is not the reason).
    rows = [
        {
            "kind": "IBKR_OPEN_ORDER",
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "ts": ts,
            "order_id": 123,
            "contract": {"symbol": "HG", "secType": "FUT", "currency": "USD", "exchange": "COMEX"},
            "order": {"action": "BUY", "orderType": "MKT", "totalQuantity": 1},
            "order_state": {"status": "Submitted"},
        }
    ]

    _write_jsonl(out, rows)
    print(json.dumps({"ok": True, "out_path": str(out), "note": "missing START/END markers"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
