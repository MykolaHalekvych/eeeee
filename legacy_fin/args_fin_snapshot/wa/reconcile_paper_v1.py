# args/wa/reconcile_paper_v1.py
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = "reconcile_paper_v1"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_jsonl_dicts(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            o = json.loads(s)
            if isinstance(o, dict):
                out.append(o)
        except Exception:
            continue
    return out


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile paper run artifacts vs IBKR snapshots.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--data-dir", default="")
    a = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    data_dir = Path(a.data_dir) if a.data_dir else (repo_root / "args" / "data")

    run_id = str(a.run_id).strip()

    sent_path = data_dir / f"sent_orders_{run_id}.jsonl"
    exec_path = data_dir / f"orders_exec_{run_id}.jsonl"
    open_orders_path = data_dir / "ibkr_open_orders_stage5.jsonl"  # default used in your Stage5
    positions_path = data_dir / f"ibkr_positions_{run_id}.jsonl"
    executions_path = data_dir / f"ibkr_executions_{run_id}.jsonl"

    sent = _read_jsonl_dicts(sent_path)
    ex = _read_jsonl_dicts(exec_path)
    oo = _read_jsonl_dicts(open_orders_path)
    pos = _read_jsonl_dicts(positions_path)
    exe = _read_jsonl_dicts(executions_path)

    # Extract submitted orders from exec log
    submitted = [r for r in ex if str(r.get("status") or "").upper() == "SUBMITTED" and r.get("order_id") is not None]
    submitted_ids = {int(r["order_id"]) for r in submitted if str(r.get("order_id")).isdigit()}

    # Open orders snapshot contains start/end markers + openOrder rows depending on snapshotter implementation.
    open_order_ids = set()
    for r in oo:
        oid = r.get("orderId") or r.get("order_id")
        if oid is not None and str(oid).isdigit():
            open_order_ids.add(int(oid))

    # Simple matching metric: submitted orderIds that appear in open orders snapshot
    matched = len(submitted_ids.intersection(open_order_ids)) if open_order_ids else 0

    # Duplicates: same send_key appears with multiple order_id in exec log
    sendkey_to_orders: Dict[str, set] = {}
    for r in submitted:
        sk = str(r.get("send_key") or "")
        oid = r.get("order_id")
        if sk and oid is not None and str(oid).isdigit():
            sendkey_to_orders.setdefault(sk, set()).add(int(oid))
    duplicates = sum(1 for sk, s in sendkey_to_orders.items() if len(s) > 1)

    report = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_z(),
        "run_id": run_id,
        "paths": {
            "sent_orders": str(sent_path),
            "orders_exec": str(exec_path),
            "open_orders": str(open_orders_path),
            "positions": str(positions_path),
            "executions": str(executions_path),
        },
        "counts": {
            "sent_orders_lines": len(sent),
            "exec_lines": len(ex),
            "submitted": len(submitted),
            "open_orders_ids": len(open_order_ids),
            "positions_lines": len(pos),
            "executions_lines": len(exe),
        },
        "metrics": {
            "submitted_ids": len(submitted_ids),
            "matched_open_orders": matched,
            "duplicates": duplicates,
        },
        "status": "PASS",
        "notes": [],
    }

    # Decide PASS/WARN/FAIL
    if duplicates > 0:
        report["status"] = "FAIL"
        report["notes"].append("duplicates_detected")

    # If there were submissions and open_orders snapshot was taken, expect matching >= 1
    if len(submitted_ids) > 0 and len(open_order_ids) > 0 and matched == 0:
        report["status"] = "WARN"
        report["notes"].append("no_match_in_open_orders_snapshot")

    out_path = data_dir / f"reconcile_report_{run_id}.json"
    _atomic_write_json(out_path, report)

    print(json.dumps({"ok": True, "status": report["status"], "out_path": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
