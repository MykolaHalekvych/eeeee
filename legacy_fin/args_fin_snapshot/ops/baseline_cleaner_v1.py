from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

SCHEMA = "baseline_cleaner_v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json_sig(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def _extract_ibkr_conn(
    cp: Dict[str, Any], fallback_client_id: int = 79
) -> Tuple[str, int, int]:
    host = "localhost"
    port = 7497
    client_id = fallback_client_id

    ibkr = cp.get("ibkr") if isinstance(cp.get("ibkr"), dict) else {}
    if isinstance(ibkr, dict):
        host = ibkr.get("host") or host
        port = int(ibkr.get("port") or port)
        client_id = int(ibkr.get("client_id") or client_id)

    if "client_id" in cp:
        try:
            client_id = int(cp["client_id"])
        except Exception:
            pass
    if "snapshot_client_id" in cp:
        try:
            client_id = int(cp["snapshot_client_id"])
        except Exception:
            pass

    return host, port, client_id


def _run_module(
    repo: Path, mod_args: List[str], timeout_s: int
) -> Tuple[int, str, str]:
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


class _App(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.errors: List[Dict[str, Any]] = []
        self.status: List[Dict[str, Any]] = []

    def nextValidId(self, orderId: int) -> None:
        self.ready.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:
        self.errors.append({"reqId": reqId, "code": errorCode, "msg": errorString})

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice,
    ) -> None:
        self.status.append(
            {
                "orderId": orderId,
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "permId": permId,
            }
        )


def _gate_check(
    cp: Dict[str, Any], stop_flag: Path, require_halt: bool
) -> Tuple[bool, str]:
    if not stop_flag.exists():
        return False, f"STOP_FLAG_REQUIRED: create {stop_flag}"
    if str(cp.get("execution_mode", "")).upper() != "PAPER":
        return False, "EXECUTION_MODE_NOT_PAPER"
    if bool(cp.get("enable_paper_execution", False)) is not True:
        return False, "ENABLE_PAPER_EXECUTION_FALSE"
    if require_halt and str(cp.get("global_mode", "")).upper() != "HALT":
        return False, "GLOBAL_MODE_NOT_HALT"
    return True, "OK"


def _attempt_cancel(
    host: str, port: int, client_id: int, order_ids: List[int], sleep_s: float
) -> Dict[str, Any]:
    attempt: Dict[str, Any] = {
        "client_id": client_id,
        "cancel_requested": [],
        "ib_status_tail": [],
        "ib_errors_tail": [],
        "connect_ok": False,
        "connect_error": None,
    }

    app = _App()
    try:
        app.connect(host, port, client_id)
        t = threading.Thread(target=app.run, daemon=True)
        t.start()

        if not app.ready.wait(timeout=10):
            attempt["connect_error"] = "no_nextValidId"
            return attempt

        attempt["connect_ok"] = True

        # NOTE: IBKR allows auto-bind only for the default client_id=0.
        # Calling reqAutoOpenOrders(True) for client_id!=0 yields code=321 noise.
        if client_id == 0:
            try:
                app.reqAutoOpenOrders(True)
            except Exception:
                pass

        for oid in order_ids:
            try:
                app.cancelOrder(int(oid), "")
            except TypeError:
                app.cancelOrder(int(oid))
            attempt["cancel_requested"].append(int(oid))

        try:
            app.reqGlobalCancel()
        except Exception:
            pass

        time.sleep(float(sleep_s))

        attempt["ib_status_tail"] = app.status[-30:]
        attempt["ib_errors_tail"] = app.errors[-30:]
        return attempt

    except Exception as e:
        attempt["connect_error"] = repr(e)
        return attempt
    finally:
        try:
            app.disconnect()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="Repo root")
    ap.add_argument("--control-plane", default="args/data/control_plane.json")
    ap.add_argument("--stop-flag", default="args/logs/stop.flag")
    ap.add_argument("--host", default="", help="Override IB host")
    ap.add_argument("--port", type=int, default=0, help="Override IB port")
    ap.add_argument(
        "--client-id", type=int, default=0, help="Override primary client id"
    )
    ap.add_argument(
        "--also-try-client-ids",
        default="79,77",
        help="Comma-separated fallback client ids to try",
    )
    ap.add_argument("--timeout-s", type=int, default=35)
    ap.add_argument("--wait-s", type=int, default=6)
    ap.add_argument("--post-cancel-sleep-s", type=float, default=6.0)
    ap.add_argument("--require-halt", action="store_true", default=True)
    ap.add_argument(
        "--allow-non-halt",
        action="store_true",
        help="If set, do not require global_mode=HALT",
    )
    ap.add_argument(
        "--open-orders-out", default="args/data/ibkr_open_orders_live.jsonl"
    )
    ap.add_argument("--report-out", default="args/data/baseline_cleaner_latest.json")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    cp_path = (repo / args.control_plane).resolve()
    stop_flag = (repo / args.stop_flag).resolve()
    out_orders = (repo / args.open_orders_out).resolve()
    report_out = (repo / args.report_out).resolve()

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": _utc_now(),
        "ok": False,
        "status": "UNKNOWN",
        "repo": str(repo),
        "control_plane": str(cp_path),
        "stop_flag": str(stop_flag),
        "before": None,
        "after": None,
        "attempts": [],
        "errors": [],
    }

    try:
        cp = _read_json_sig(cp_path)
    except Exception as e:
        report["errors"].append({"where": "read_control_plane", "error": repr(e)})
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 2

    require_halt = not args.allow_non_halt
    gate_ok, gate_reason = _gate_check(cp, stop_flag, require_halt=require_halt)
    if not gate_ok:
        report["status"] = "BLOCKED"
        report["blocked_reason"] = gate_reason
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 2

    host, port, primary_client_id = _extract_ibkr_conn(cp, fallback_client_id=79)
    if args.host:
        host = args.host
    if args.port:
        port = int(args.port)
    if args.client_id:
        primary_client_id = int(args.client_id)

    # Snapshot BEFORE (read-only)
    mod = [
        "args.ibkr.ibkr_open_orders_snapshotter_v0",
        "--host",
        host,
        "--port",
        str(port),
        "--client-id",
        str(primary_client_id),
        "--timeout-s",
        str(args.timeout_s),
        "--wait-s",
        str(args.wait_s),
        "--out",
        str(out_orders),
    ]
    rc, stdout, stderr = _run_module(repo, mod, timeout_s=max(15, args.timeout_s + 20))
    if rc != 0:
        report["status"] = "INFRA_FAIL"
        report["errors"].append(
            {
                "where": "open_orders_snapshot_before",
                "rc": rc,
                "stderr": stderr,
                "stdout": stdout[:300],
            }
        )
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 2

    before_orders = _parse_open_orders_jsonl(out_orders)
    order_ids = [
        o.get("order_id") for o in before_orders if isinstance(o.get("order_id"), int)
    ]
    report["before"] = {
        "count": len(before_orders),
        "statuses": sorted(
            {
                (o.get("order_state") or {}).get("status")
                for o in before_orders
                if (o.get("order_state") or {}).get("status")
            }
        ),
        "order_ids": order_ids,
        "out_path": str(out_orders),
        "conn": {"host": host, "port": port, "client_id": primary_client_id},
    }

    if not order_ids:
        report["ok"] = True
        report["status"] = "ALREADY_CLEAN"
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0

    # Candidate client_ids: primary + fallbacks (unique)
    candidates: List[int] = []

    def _add(cid: int) -> None:
        if cid not in candidates:
            candidates.append(cid)

    _add(primary_client_id)
    for tok in (args.also_try_client_ids or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            _add(int(tok))
        except Exception:
            continue

    for cid in candidates:
        report["attempts"].append(
            _attempt_cancel(
                host, port, cid, order_ids, sleep_s=float(args.post_cancel_sleep_s)
            )
        )

    # Snapshot AFTER (read-only)
    rc2, stdout2, stderr2 = _run_module(
        repo, mod, timeout_s=max(15, args.timeout_s + 20)
    )
    if rc2 != 0:
        report["status"] = "INFRA_FAIL"
        report["errors"].append(
            {
                "where": "open_orders_snapshot_after",
                "rc": rc2,
                "stderr": stderr2,
                "stdout": stdout2[:300],
            }
        )
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        return 2

    after_orders = _parse_open_orders_jsonl(out_orders)
    statuses_after = sorted(
        {
            (o.get("order_state") or {}).get("status")
            for o in after_orders
            if (o.get("order_state") or {}).get("status")
        }
    )
    report["after"] = {
        "count": len(after_orders),
        "statuses": statuses_after,
        "order_ids": [
            o.get("order_id") for o in after_orders if o.get("order_id") is not None
        ],
        "out_path": str(out_orders),
    }

    if len(after_orders) == 0:
        report["ok"] = True
        report["status"] = "CLEARED"
        rc_out = 0
    else:
        if statuses_after and all(s == "PendingCancel" for s in statuses_after):
            report["status"] = "NOT_CLEARED_WAIT_MARKET_OPEN"
        else:
            report["status"] = "NOT_CLEARED"
        report["ok"] = False
        rc_out = 1

    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return rc_out


if __name__ == "__main__":
    raise SystemExit(main())
