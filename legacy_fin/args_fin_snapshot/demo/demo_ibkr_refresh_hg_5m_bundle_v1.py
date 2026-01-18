from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


SCHEMA = "demo_ibkr_refresh_hg_5m_bundle_v1c"

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

CONTRACT_PATH = DATA_DIR / "ibkr_mhg_contract_v1.json"
CSV_PATH = DATA_DIR / "mhg_5m_bars_ibkr.csv"
CSV_META_PATH = DATA_DIR / "mhg_5m_bars_ibkr.meta.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"RUN: {title}")
    print("=" * 72)


def _run_module(mod: str) -> int:
    _banner(mod)
    # Use the same interpreter as the caller (py -3.11 -m ...)
    proc = subprocess.run([sys.executable, "-m", mod], cwd=str(REPO_ROOT))
    return int(proc.returncode)


def _stdout_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    ts = _utc_now_iso()
    warnings: List[str] = []

    # 1) Contract resolve (best-effort)
    rc_contract = _run_module("args.demo.demo_ibkr_contract_resolve_mhg")

    if rc_contract == 0:
        pass
    elif rc_contract == 1:
        # WARN (nextValidId handshake) — tolerate if we have cached contract
        warnings.append("contract_resolve_warn_nextValidId_timeout")
        if not CONTRACT_PATH.exists():
            out: Dict[str, Any] = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": "contract_resolve_warn_but_no_cached_contract",
                "paths": {"contract": str(CONTRACT_PATH)},
                "warnings": warnings,
            }
            _stdout_json(out)
            return 2
        print(
            f"WARN: contract resolve returned rc=1; using cached contract: {CONTRACT_PATH}"
        )
    else:
        # Hard fail
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "severity": "FAIL",
            "error": f"contract_resolve_failed_rc={rc_contract}",
            "paths": {"contract": str(CONTRACT_PATH)},
            "warnings": warnings,
        }
        _stdout_json(out)
        return 2

    # 2) Fetch bars (best-effort)
    rc_fetch = _run_module("args.demo.demo_ibkr_fetch_mhg_5m")

    if rc_fetch == 0:
        pass
    else:
        warnings.append(f"fetch_bars_failed_rc={rc_fetch}")
        if not CSV_PATH.exists():
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": "fetch_bars_failed_and_no_cached_csv",
                "paths": {"csv": str(CSV_PATH)},
                "warnings": warnings,
            }
            _stdout_json(out)
            return 2
        print(f"WARN: fetch bars failed (rc={rc_fetch}); using cached csv: {CSV_PATH}")

    # 3) Attach contract meta (best-effort)
    rc_meta = _run_module("args.demo.demo_attach_contract_meta_mhg")

    if rc_meta == 0:
        pass
    else:
        warnings.append(f"attach_meta_failed_rc={rc_meta}")
        if not CSV_META_PATH.exists():
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "severity": "FAIL",
                "error": "attach_meta_failed_and_no_cached_meta",
                "paths": {"meta": str(CSV_META_PATH)},
                "warnings": warnings,
            }
            _stdout_json(out)
            return 2
        print(
            f"WARN: attach meta failed (rc={rc_meta}); using cached meta: {CSV_META_PATH}"
        )

    print("\nDONE: MHG bundle refreshed (best-effort).")

    out_ok = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": True,
        "exit_code": 0,
        "severity": "OK" if not warnings else "OK_WITH_WARNINGS",
        "warnings": warnings,
        "paths": {
            "contract": str(CONTRACT_PATH),
            "csv": str(CSV_PATH),
            "csv_meta": str(CSV_META_PATH),
        },
    }
    _stdout_json(out_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
