from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "baseline_check_v1"

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _read_json_sig(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_bytes().decode("utf-8-sig"))

def _extract_ibkr_conn(cp: Dict[str, Any], fallback_client_id: int = 79) -> Tuple[str, int, int]:
    host = "localhost"
    port = 7497
    client_id = fallback_client_id

    ibkr = cp.get("ibkr") if isinstance(cp.get("ibkr"), dict) else {}
    if isinstance(ibkr, dict):
        host = ibkr.get("host") or host
        port = int(ibkr.get("port") or port)
        client_id = int(ibkr.get("client_id") or client_id)

    # Common fallbacks
    if "client_id" in cp:
        try: client_id = int(cp["client_id"])
        except Exception: pass
    if "snapshot_client_id" in cp:
        try: client_id = int(cp["snapshot_client_id"])
        except Exception: pass

    return host, port, client_id

def _run_module(repo: Path, mod_args: List[str], timeout_s: int) -> Tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, "-m", *mod_args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

def _parse_open_orders_jsonl(path: Path) -> List[Dict[str, Any]]:
    orders: List[Dict[str, Any]] = []
    if not path.exists():
        return orders
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("kind") == "IBKR_OPEN_ORDER":
            orders.append(obj)
    return orders

def _summarize_orders(raw: List[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for o in raw[:limit]:
        order = o.get("order") or {}
        st = (o.get("order_state") or {}).get("status")
        out.append({
            "order_id": o.get("order_id"),
            "status": st,
            "orderType": order.get("orderType"),
            "action": order.get("action"),
            "tif": order.get("tif"),
            "lmtPrice": order.get("lmtPrice"),
            "totalQuantity": order.get("totalQuantity"),
        })
    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="Repo root")
    ap.add_argument("--control-plane", default="args/data/control_plane.json")
    ap.add_argument("--host", default="", help="Override IB host")
    ap.add_argument("--port", type=int, default=0, help="Override IB port")
    ap.add_argument("--client-id", type=int, default=0, help="Override IB client id")
    ap.add_argument("--timeout-s", type=int, default=35)
    ap.add_argument("--wait-s", type=int, default=6)
    ap.add_argument("--open-orders-out", default="args/data/ibkr_open_orders_live.jsonl")
    ap.add_argument("--with-positions", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    cp_path = (repo / args.control_plane).resolve()

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": _utc_now(),
        "ok": False,
        "repo": str(repo),
        "control_plane": str(cp_path),
        "open_orders": {"count": None, "status": None, "sample": []},
        "positions": None,
        "errors": [],
    }

    try:
        cp = _read_json_sig(cp_path)
    except Exception as e:
        report["errors"].append({"where": "read_control_plane", "error": repr(e)})
        print(json.dumps(report, ensure_ascii=False))
        return 2

    host, port, client_id = _extract_ibkr_conn(cp, fallback_client_id=79)
    if args.host: host = args.host
    if args.port: port = int(args.port)
    if args.client_id: client_id = int(args.client_id)

    out_path = (repo / args.open_orders_out).resolve()

    # Open orders snapshot (read-only)
    mod = [
        "args.ibkr.ibkr_open_orders_snapshotter_v0",
        "--host", host,
        "--port", str(port),
        "--client-id", str(client_id),
        "--timeout-s", str(args.timeout_s),
        "--wait-s", str(args.wait_s),
        "--out", str(out_path),
    ]

    rc, stdout, stderr = _run_module(repo, mod, timeout_s=max(10, args.timeout_s + 15))
    if rc != 0:
        report["errors"].append({"where": "open_orders_snapshot", "rc": rc, "stderr": stderr, "stdout": stdout[:300]})
        print(json.dumps(report, ensure_ascii=False))
        return 2

    raw_orders = _parse_open_orders_jsonl(out_path)
    statuses = sorted({(o.get("order_state") or {}).get("status") for o in raw_orders if (o.get("order_state") or {}).get("status")})
    count = len(raw_orders)

    # Determine baseline status
    if count == 0:
        status = "CLEAN"
        ok = True
    else:
        if statuses and all(s == "PendingCancel" for s in statuses):
            status = "DIRTY_PENDING_CANCEL"
        else:
            status = "DIRTY"
        ok = False

    report["open_orders"] = {
        "count": count,
        "status": status,
        "statuses": statuses,
        "sample": _summarize_orders(raw_orders, limit=50),
        "out_path": str(out_path),
        "conn": {"host": host, "port": port, "client_id": client_id},
    }
    report["ok"] = ok

    # Optional positions snapshot (still read-only)
    if args.with_positions:
        mod2 = [
            "args.ibkr.ibkr_positions_snapshotter_v0",
            "--host", host,
            "--port", str(port),
            "--client-id", str(client_id),
            "--timeout-s", str(max(12, args.timeout_s)),
        ]
        rc2, stdout2, stderr2 = _run_module(repo, mod2, timeout_s=max(15, args.timeout_s + 20))
        if rc2 == 0:
            try:
                report["positions"] = json.loads(stdout2) if stdout2 else {"note": "no stdout"}
            except Exception:
                report["positions"] = {"note": "positions stdout not json", "stdout_head": (stdout2 or "")[:300]}
        else:
            report["positions"] = {"rc": rc2, "stderr": (stderr2 or "")[:300]}

    print(json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
