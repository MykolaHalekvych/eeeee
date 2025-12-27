from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from args.ibkr.ibkr_exec_ledger_v1 import compute_send_key


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    yield obj
            except Exception:
                continue


def reconcile_sendplan_vs_exec(
    *,
    sendplan_path: Path,
    exec_log_path: Path,
    run_id: str,
    out_json_path: Path | None = None,
) -> Dict[str, Any]:
    send_keys: Dict[str, Dict[str, Any]] = {}
    for item in iter_jsonl(sendplan_path):
        k = compute_send_key(run_id, item)
        send_keys[k] = item

    latest_status: Dict[str, str] = {}
    for ev in iter_jsonl(exec_log_path):
        k = str(ev.get("send_key") or "").strip()
        st = str(ev.get("status") or "").strip()
        if k:
            latest_status[k] = st or latest_status.get(k, "")

    missing = sorted([k for k in send_keys.keys() if k not in latest_status])
    present = sorted([k for k in send_keys.keys() if k in latest_status])

    out = {
        "ts": _now_utc_iso(),
        "run_id": run_id,
        "sendplan_path": str(sendplan_path),
        "exec_log_path": str(exec_log_path),
        "counts": {"sendplan": len(send_keys), "exec_present": len(present), "exec_missing": len(missing)},
        "missing_send_keys": missing[:200],
    }

    if out_json_path is not None:
        out_json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return out
