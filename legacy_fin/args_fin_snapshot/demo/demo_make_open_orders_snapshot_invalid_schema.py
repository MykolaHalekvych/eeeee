from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_jsonl(path: Path, rows: list[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(
                json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(prog="demo_make_open_orders_snapshot_invalid_schema")
    ap.add_argument("--out", default="args/data/ibkr_open_orders_invalid_schema.jsonl")
    args = ap.parse_args()

    repo = _repo_root()
    out = Path(str(args.out))
    if not out.is_absolute():
        out = repo / out

    ts = _iso_utc_now()

    rows = [
        {
            "kind": "IBKR_SNAPSHOT_START",
            "schema_version": "WRONG_SCHEMA",
            "ts": ts,
        },
        {
            "kind": "IBKR_OPEN_ORDER",
            "schema_version": "WRONG_SCHEMA",
            "ts": ts,
            "order_id": 123,
            "contract": {
                "symbol": "HG",
                "secType": "FUT",
                "currency": "USD",
                "exchange": "COMEX",
            },
            "order": {"action": "BUY", "orderType": "MKT", "totalQuantity": 1},
            "order_state": {"status": "Submitted"},
        },
        {
            "kind": "IBKR_SNAPSHOT_END",
            "schema_version": "WRONG_SCHEMA",
            "ts": ts,
        },
    ]

    _write_jsonl(out, rows)
    print(
        json.dumps(
            {"ok": True, "out_path": str(out), "note": "invalid schema_version"},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
