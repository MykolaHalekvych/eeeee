# args/wa/reconcile_paper_v1b.py
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


SCHEMA_VERSION = "reconcile_paper_v1b"

INFO_CODES = {2104, 2106, 2158}   # IBKR “farm connection OK” style info
WARN_CODES = {399}                # IBKR warning (e.g., outside trading hours)


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    stats = {"lines": 0, "dicts": 0, "parse_errors": 0}
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out, stats
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stats["lines"] += 1
        s = line.strip()
        if not s:
            continue
        try:
            o = json.loads(s)
            if isinstance(o, dict):
                out.append(o)
                stats["dicts"] += 1
            else:
                stats["parse_errors"] += 1
        except Exception:
            stats["parse_errors"] += 1
    return out, stats


def _as_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        s = str(x).strip()
        if s.isdigit():
            return int(s)
    except Exception:
        return None
    return None


def _pick(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d.get(k)
    return None


@dataclass(frozen=True)
class CodeBuckets:
    info: int
    warn: int
    error: int
    info_samples: List[Dict[str, Any]]
    warn_samples: List[Dict[str, Any]]
    error_samples: List[Dict[str, Any]]


def _bucket_ibkr_errors(exec_rows: List[Dict[str, Any]]) -> CodeBuckets:
    info = warn = err = 0
    info_s: List[Dict[str, Any]] = []
    warn_s: List[Dict[str, Any]] = []
    err_s: List[Dict[str, Any]] = []

    for r in exec_rows:
        if str(r.get("kind") or "") != "IBKR_EVENT":
            continue
        if str(r.get("event") or "") != "error":
            continue

        code = _as_int(r.get("code"))
        if code is None:
            # unknown code → treat as error
            err += 1
            if len(err_s) < 8:
                err_s.append({"code": r.get("code"), "msg": r.get("msg"), "reqId": r.get("reqId")})
            continue

        if code in INFO_CODES:
            info += 1
            if len(info_s) < 5:
                info_s.append({"code": code, "msg": r.get("msg")})
        elif code in WARN_CODES:
            warn += 1
            if len(warn_s) < 8:
                warn_s.append({"code": code, "msg": r.get("msg"), "order_id": r.get("order_id") or r.get("reqId")})
        else:
            err += 1
            if len(err_s) < 8:
                err_s.append({"code": code, "msg": r.get("msg"), "order_id": r.get("order_id") or r.get("reqId")})

    return CodeBuckets(info=info, warn=warn, error=err, info_samples=info_s, warn_samples=warn_s, error_samples=err_s)


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile v1B: match runtime SUBMITTED vs IBKR open orders / executions; filter info codes.")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--open-orders-path", default="ibkr_open_orders_stage5.jsonl")
    ap.add_argument("--positions-path", default="")
    ap.add_argument("--executions-path", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    data_dir = Path(a.data_dir) if a.data_dir else (repo_root / "args" / "data")

    run_id = str(a.run_id).strip()

    sent_path = data_dir / f"sent_orders_{run_id}.jsonl"
    exec_path = data_dir / f"orders_exec_{run_id}.jsonl"
    open_orders_path = data_dir / str(a.open_orders_path)
    positions_path = Path(a.positions_path) if a.positions_path else (data_dir / f"ibkr_positions_{run_id}.jsonl")
    executions_path = Path(a.executions_path) if a.executions_path else (data_dir / f"ibkr_executions_{run_id}.jsonl")

    # Load
    sent_rows, sent_stats = _read_jsonl(sent_path)
    exec_rows, exec_stats = _read_jsonl(exec_path)
    oo_rows, oo_stats = _read_jsonl(open_orders_path)
    pos_rows, pos_stats = _read_jsonl(positions_path)
    exe_rows, exe_stats = _read_jsonl(executions_path)

    # Submitted from runtime exec log
    submitted = [r for r in exec_rows if str(r.get("kind") or "") == "EXEC_EVENT" and str(r.get("status") or "").upper() == "SUBMITTED"]
    submitted_ids: Set[int] = set()
    sendkey_to_orders: Dict[str, Set[int]] = {}

    for r in submitted:
        oid = _as_int(r.get("order_id"))
        if oid is None:
            continue
        submitted_ids.add(oid)
        sk = str(r.get("send_key") or "")
        if sk:
            sendkey_to_orders.setdefault(sk, set()).add(oid)

    duplicates = sum(1 for sk, s in sendkey_to_orders.items() if len(s) > 1)

    # Runtime IBKR events (openOrder/orderStatus)
    runtime_open_ids: Set[int] = set()
    runtime_status_ids: Set[int] = set()
    runtime_status_by_id: Dict[int, str] = {}

    for r in exec_rows:
        if str(r.get("kind") or "") != "IBKR_EVENT":
            continue
        ev = str(r.get("event") or "")
        if ev == "openOrder":
            oid = _as_int(r.get("order_id"))
            if oid is not None:
                runtime_open_ids.add(oid)
        elif ev == "orderStatus":
            oid = _as_int(r.get("order_id"))
            if oid is not None:
                runtime_status_ids.add(oid)
                runtime_status_by_id[oid] = str(r.get("status") or "")

    runtime_seen_ids = runtime_open_ids.union(runtime_status_ids)

    # Open orders snapshot (from snapshotter)
    snapshot_open_ids: Set[int] = set()
    for r in oo_rows:
        oid = _as_int(_pick(r, "orderId", "order_id"))
        if oid is not None:
            snapshot_open_ids.add(oid)

    # Executions snapshot
    exec_fill_ids: Set[int] = set()
    for r in exe_rows:
        if str(r.get("kind") or "") != "IBKR_EXECUTION":
            continue
        oid = _as_int(r.get("orderId"))
        if oid is not None:
            exec_fill_ids.add(oid)

    matched_runtime = len(submitted_ids.intersection(runtime_seen_ids))
    matched_snapshot = len(submitted_ids.intersection(snapshot_open_ids))
    matched_execs = len(submitted_ids.intersection(exec_fill_ids))

    matched_any_ids = submitted_ids.intersection(runtime_seen_ids.union(snapshot_open_ids).union(exec_fill_ids))
    dangling = sorted(list(submitted_ids.difference(matched_any_ids)))

    # Detect cancel_all intent in runtime exec log
    cancel_all_sent = any(str(r.get("kind") or "") == "EXEC_EVENT" and str(r.get("status") or "").upper() == "CANCEL_ALL_SENT" for r in exec_rows)
    cancel_all_sim = any(str(r.get("kind") or "") == "EXEC_EVENT" and str(r.get("status") or "").upper() == "CANCEL_ALL_SIM" for r in exec_rows)

    # Error buckets (filter info)
    buckets = _bucket_ibkr_errors(exec_rows)

    # Ratios
    submitted_n = len(submitted_ids)
    def _ratio(x: int, n: int) -> float:
        return float(x) / float(n) if n > 0 else 0.0

    # Status decision
    status = "PASS"
    notes: List[str] = []

    if duplicates > 0:
        status = "FAIL"
        notes.append("duplicates_detected")

    if buckets.error > 0 and status != "FAIL":
        status = "WARN"
        notes.append("ibkr_error_codes_present")

    if submitted_n > 0:
        if len(matched_any_ids) == 0 and status != "FAIL":
            status = "WARN"
            notes.append("no_match_anywhere_for_submitted")
        if len(dangling) > 0 and (not cancel_all_sent) and status == "PASS":
            status = "WARN"
            notes.append("dangling_submitted_present_no_cancel_all")

        # If open orders snapshot is empty and cancel_all happened, dangling is expected.
        if len(dangling) > 0 and cancel_all_sent:
            notes.append("dangling_expected_due_to_cancel_all")

    report = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_z(),
        "run_id": run_id,
        "paths": {
            "sent_orders": str(sent_path),
            "orders_exec": str(exec_path),
            "open_orders_snapshot": str(open_orders_path),
            "positions_snapshot": str(positions_path),
            "executions_snapshot": str(executions_path),
        },
        "read_stats": {
            "sent": sent_stats,
            "exec": exec_stats,
            "open_orders": oo_stats,
            "positions": pos_stats,
            "executions": exe_stats,
        },
        "counts": {
            "submitted": submitted_n,
            "runtime_open_ids": len(runtime_open_ids),
            "runtime_status_ids": len(runtime_status_ids),
            "snapshot_open_ids": len(snapshot_open_ids),
            "execution_fills_ids": len(exec_fill_ids),
            "positions_lines": len(pos_rows),
        },
        "match": {
            "matched_runtime": matched_runtime,
            "matched_snapshot": matched_snapshot,
            "matched_execs": matched_execs,
            "ratio_runtime": _ratio(matched_runtime, submitted_n),
            "ratio_snapshot": _ratio(matched_snapshot, submitted_n),
            "ratio_execs": _ratio(matched_execs, submitted_n),
            "matched_any": len(matched_any_ids),
            "ratio_any": _ratio(len(matched_any_ids), submitted_n),
            "dangling_submitted_ids": dangling[:50],
            "dangling_count": len(dangling),
        },
        "idempotency": {
            "duplicates": duplicates,
            "send_keys_with_multiple_order_ids": [sk for sk, s in sendkey_to_orders.items() if len(s) > 1][:20],
        },
        "cancel_all": {
            "cancel_all_sent": bool(cancel_all_sent),
            "cancel_all_sim": bool(cancel_all_sim),
        },
        "ibkr_codes": {
            "info": buckets.info,
            "warn": buckets.warn,
            "error": buckets.error,
            "info_samples": buckets.info_samples,
            "warn_samples": buckets.warn_samples,
            "error_samples": buckets.error_samples,
        },
        "status": status,
        "notes": notes,
    }

    out_path = Path(a.out) if str(a.out).strip() else (data_dir / f"reconcile_report_{run_id}_v1b.json")
    _atomic_write_json(out_path, report)
    print(json.dumps({"ok": True, "status": status, "out_path": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
