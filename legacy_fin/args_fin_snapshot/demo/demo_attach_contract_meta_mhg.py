from __future__ import annotations

import json
from pathlib import Path

from args.ibkr.ibkr_contract_resolver_v1 import utc_now_str

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTRACT_PATH = REPO_ROOT / "args" / "data" / "ibkr_mhg_contract_v1.json"
CSV_PATH = REPO_ROOT / "args" / "data" / "mhg_5m_bars_ibkr.csv"
OUT_META_PATH = REPO_ROOT / "args" / "data" / "mhg_5m_bars_ibkr.meta.json"


def main() -> int:
    if not CONTRACT_PATH.exists():
        print(f"Missing contract file: {CONTRACT_PATH}")
        return 2
    if not CSV_PATH.exists():
        print(f"Missing CSV file: {CSV_PATH}")
        return 3

    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    picked = payload.get("picked") or payload

    conid = picked.get("conId")
    local = picked.get("localSymbol")
    if not conid or not local:
        print("Contract JSON missing picked.conId or picked.localSymbol")
        return 4

    meta = {
        "generated_at_utc": utc_now_str(),
        "source_contract_file": str(CONTRACT_PATH),
        "csv_path": str(CSV_PATH),
        "contract": {
            "conId": conid,
            "localSymbol": local,
            "symbol": picked.get("symbol"),
            "secType": picked.get("secType"),
            "exchange": picked.get("exchange"),
            "currency": picked.get("currency"),
            "multiplier": picked.get("multiplier"),
            "lastTradeDateOrContractMonth": picked.get("lastTradeDateOrContractMonth"),
            "tradingClass": picked.get("tradingClass"),
        },
    }

    OUT_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: wrote {OUT_META_PATH}")
    print(f"Contract: conId={conid} local={local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


