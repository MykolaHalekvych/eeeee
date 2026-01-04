from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pick_latest_report() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    reports = sorted(
        [p for p in LOGS_DIR.iterdir() if p.is_file() and p.name.startswith("run_report_") and p.name.endswith("_paper.json")],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def _dig_contract(rr: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    inputs = rr.get("inputs") if isinstance(rr.get("inputs"), dict) else {}
    csv_meta_path = inputs.get("csv_meta_path")
    csv_meta = inputs.get("csv_meta") if isinstance(inputs.get("csv_meta"), dict) else None

    conid = None
    local = None
    if csv_meta and isinstance(csv_meta.get("contract"), dict):
        c = csv_meta["contract"]
        conid = str(c.get("conId")) if c.get("conId") is not None else None
        local = str(c.get("localSymbol")) if c.get("localSymbol") is not None else None

    return (
        str(csv_meta_path) if csv_meta_path else None,
        conid,
        local,
    )


def main() -> int:
    rp = _pick_latest_report()
    if rp is None:
        print("FAIL: No run_report_*_paper.json found in args/logs")
        return 2

    rr = _load_json(rp)
    if rr is None:
        print(f"FAIL: Could not parse JSON: {rp}")
        return 3

    csv_path = None
    inputs = rr.get("inputs") if isinstance(rr.get("inputs"), dict) else {}
    if inputs.get("csv_path"):
        csv_path = str(inputs.get("csv_path"))

    csv_meta_path, conid, local = _dig_contract(rr)

    print(f"Latest report: {rp}")
    print(f"csv_path: {csv_path}")
    print(f"csv_meta_path: {csv_meta_path}")
    print(f"contract.conId: {conid}")
    print(f"contract.localSymbol: {local}")

    if csv_meta_path and conid and local:
        print("PASS: run_report contains contract identity (conId/localSymbol).")
        return 0

    print("FAIL: run_report missing csv_meta_path and/or contract identity.")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
