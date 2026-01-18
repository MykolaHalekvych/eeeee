from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run(cmd: List[str], timeout_s: int) -> int:
    try:
        cp = subprocess.run(
            cmd,
            timeout=timeout_s,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return int(cp.returncode)
    except Exception:
        return 2


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ownership gate (v0): halt on unknown positions/orders."
    )
    ap.add_argument(
        "--control",
        default="",
        help="control_plane.json (default args/data/control_plane.json)",
    )
    ap.add_argument(
        "--write-out",
        default="",
        help="ownership_status.json (default args/data/ownership_status.json)",
    )
    ap.add_argument("--timeout-s", type=int, default=25)
    args = ap.parse_args()

    repo = _repo_root()
    control_path = (
        Path(args.control)
        if args.control
        else (repo / "args" / "data" / "control_plane.json")
    )
    out_path = (
        Path(args.write_out)
        if args.write_out
        else (repo / "args" / "data" / "ownership_status.json")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    control = _read_json(control_path) or {}
    allow = set(control.get("instrument_allowlist") or [])
    prefix = (control.get("ownership") or {}).get("order_ref_prefix") or "ARGS|"
    startup = control.get("startup") or {}
    halt_pos = bool(startup.get("halt_if_unknown_positions", True))
    halt_ord = bool(startup.get("halt_if_unknown_orders", True))

    ibkr = control.get("ibkr") or {}
    host = str(ibkr.get("host", "localhost"))
    port = int(ibkr.get("port", 7497))
    client_id = int(ibkr.get("client_id", 51))
    timeout_s = int(ibkr.get("timeout_s", args.timeout_s))

    # 1) snapshot positions
    pos_path = repo / "args" / "data" / "ibkr_positions_live.json"
    rc_pos = _run(
        [
            "py",
            "-3.11",
            "-m",
            "args.ibkr.ibkr_positions_snapshotter_v0",
            "--host",
            host,
            "--port",
            str(port),
            "--client-id",
            str(client_id),
            "--timeout-s",
            str(timeout_s),
            "--out",
            str(pos_path),
        ],
        timeout_s=timeout_s + 5,
    )
    pos = _read_json(pos_path) or {}

    # 2) snapshot open orders (if tool exists)
    ord_path = repo / "args" / "data" / "ibkr_open_orders_live.jsonl"
    rc_ord = 0
    orders: List[Dict[str, Any]] = []
    # best-effort: try existing snapshotter if present
    rc_ord = _run(
        [
            "py",
            "-3.11",
            "-m",
            "args.ibkr.ibkr_open_orders_snapshotter_v0",
            "--host",
            host,
            "--port",
            str(port),
            "--client-id",
            str(client_id),
            "--timeout-s",
            str(timeout_s),
            "--wait-s",
            "5",
            "--out",
            str(ord_path),
        ],
        timeout_s=timeout_s + 10,
    )
    if ord_path.exists():
        try:
            for line in ord_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if line.strip():
                    orders.append(json.loads(line))
        except Exception:
            orders = []

    # classify
    unknown_positions: List[Dict[str, Any]] = []
    for r in pos.get("rows") or []:
        try:
            sym = str(r.get("symbol") or "")
            qty = float(r.get("position") or 0.0)
            if abs(qty) > 0 and allow and (sym not in allow):
                unknown_positions.append(
                    {
                        "symbol": sym,
                        "position": qty,
                        "secType": r.get("secType"),
                        "currency": r.get("currency"),
                    }
                )
        except Exception:
            continue

    unknown_orders: List[Dict[str, Any]] = []
    for o in orders:
        # heuristic: if order_ref present and doesn't start with prefix -> unknown
        ref = str(o.get("order_ref") or o.get("orderRef") or "")
        sym = str(o.get("symbol") or o.get("contract", {}).get("symbol") or "")
        if ref and not ref.startswith(prefix):
            unknown_orders.append({"symbol": sym, "order_ref": ref})
        # also treat non-allowlisted symbols as unknown
        if allow and sym and (sym not in allow):
            unknown_orders.append({"symbol": sym, "order_ref": ref})

    level = "PASS"
    exit_code = 0
    reason = "OK"
    failures: List[str] = []
    warnings: List[str] = []

    if rc_pos != 0:
        failures.append("POSITIONS_SNAPSHOT_FAIL")
    if rc_ord != 0:
        warnings.append("OPEN_ORDERS_SNAPSHOT_WARN")

    if unknown_positions and halt_pos:
        failures.append("UNKNOWN_POSITIONS_PRESENT")
    elif unknown_positions:
        warnings.append("UNKNOWN_POSITIONS_PRESENT")

    if unknown_orders and halt_ord:
        failures.append("UNKNOWN_ORDERS_PRESENT")
    elif unknown_orders:
        warnings.append("UNKNOWN_ORDERS_PRESENT")

    if failures:
        level, exit_code, reason = "FAIL", 2, failures[0]
    elif warnings:
        level, exit_code, reason = "WARN", 1, warnings[0]

    out = {
        "schema": "ownership_gate_v0",
        "ts_utc": _iso_utc_now(),
        "ok": (exit_code != 2),
        "level": level,
        "exit_code": exit_code,
        "reason": reason,
        "control_plane": str(control_path),
        "allowlist": sorted(list(allow)),
        "order_ref_prefix": prefix,
        "positions_snapshot": str(pos_path),
        "open_orders_snapshot": str(ord_path),
        "unknown_positions": unknown_positions,
        "unknown_orders": unknown_orders,
        "warnings": warnings,
        "failures": failures,
    }

    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
