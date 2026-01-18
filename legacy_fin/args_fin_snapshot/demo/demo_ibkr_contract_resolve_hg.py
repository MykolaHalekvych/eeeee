from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from args.ibkr.ibkr_contract_resolver_v1 import (
    contract_details_to_dict,
    fetch_contract_details,
    load_ibkr_connection,
    make_hg_fut_contract,
    select_front_month,
    utc_now_str,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONN_PATH = REPO_ROOT / "args" / "data" / "ibkr_connection_v0.json"
OUT_PATH = REPO_ROOT / "args" / "data" / "ibkr_hg_contract_v1.json"


def _print_candidates(rows: List[Dict[str, Any]], limit: int = 12) -> None:
    print("")
    print(f"Candidates (showing up to {limit}):")
    for i, r in enumerate(rows[:limit], start=1):
        print(
            f"{i:02d}. conId={r.get('conId')}  local={r.get('localSymbol')}  "
            f"lastTrade={r.get('lastTradeDateOrContractMonth')}  exch={r.get('exchange')}  "
            f"ccy={r.get('currency')}  mult={r.get('multiplier')}"
        )


def main() -> int:
    conn = load_ibkr_connection(CONN_PATH)

    print("IBKR Contract Resolver v1 — HG (Copper)")
    print(f"Conn: host={conn.host} port={conn.port} client_id={conn.client_id}")
    print(f"Config: {CONN_PATH}")

    base = make_hg_fut_contract(exchange="COMEX", currency="USD")
    details, errors = fetch_contract_details(conn, base, timeout_s=25.0)

    if errors:
        print("")
        print("IBKR errors (may still be OK if details exist):")
        for reqId, code, msg in errors[:8]:
            print(f"- reqId={reqId} code={code} msg={msg}")

    if not details:
        print("")
        print(
            "No contract details returned. Ensure TWS/IB Gateway is running and API port is correct."
        )
        return 2

    rows = [contract_details_to_dict(cd) for cd in details]
    _print_candidates(rows, limit=12)

    picked = select_front_month(details)
    if picked is None:
        print("")
        print(
            "Could not select front-month (no parseable lastTradeDateOrContractMonth)."
        )
        return 3

    picked_row = contract_details_to_dict(picked)

    payload = {
        "resolved_at_utc": utc_now_str(),
        "query": {
            "secType": "FUT",
            "symbol": "HG",
            "exchange": "COMEX",
            "currency": "USD",
        },
        "selection": {"method": "front_month_by_lastTradeDateOrContractMonth"},
        "picked": picked_row,
        "candidates_count": len(rows),
    }

    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("")
    print(f"OK: wrote {OUT_PATH}")
    print(
        f"Picked: conId={picked_row.get('conId')} local={picked_row.get('localSymbol')} "
        f"lastTrade={picked_row.get('lastTradeDateOrContractMonth')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
