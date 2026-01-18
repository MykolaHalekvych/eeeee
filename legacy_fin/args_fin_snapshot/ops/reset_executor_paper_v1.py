# args/ops/reset_executor_paper_v1.py
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order

from args.ibkr.order_sanitize_v0 import sanitize_order_v0

from args.ops.reset_ledger_v0 import (
    LedgerEntry,
    build_action_key,
    load_ledger,
    save_ledger,
    upsert_entry,
    get_entry,
)

from args.ops.reset_events_v0 import append_event

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

STOP_FLAG = DATA_DIR / "stop.flag"
SCHEMA = "reset_executor_paper_v1"

# Codes
INFO_CODES = {2104, 2106, 2158}  # farm connections, etc
WARN_CODES = {399}  # "will not be placed until ..." (accepted, not failure)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _lock_or_fail(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(f"ACTIVE_LOCK: {lock_path}")


def _unlock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


# ----------------------------
# Ledger monotonic updates
# ----------------------------
_STATE_RANK = {
    "PLANNED": 10,
    "BLOCKED": 20,
    "SKIPPED": 30,
    "SENT": 40,
    "ACK": 50,
    "FILLED": 60,
    "REJECTED": 60,
    "CANCELLED": 60,
}


def _rank(state: str) -> int:
    return _STATE_RANK.get(_u(state), 0)


def _ledger_upsert_monotonic(
    ledger: Dict[str, Any],
    *,
    key: str,
    kind: str,
    state: str,
    ts_utc: str,
    details: Dict[str, Any],
) -> None:
    prev = get_entry(ledger, key)
    prev_state = str(prev.get("state") or "") if prev else ""
    # Do not downgrade states (e.g., SENT -> PLANNED)
    if prev and _rank(prev_state) > _rank(state):
        return
    upsert_entry(
        ledger,
        LedgerEntry(
            key=key,
            kind=kind,
            state=state,
            ts_utc=ts_utc,
            details=details,
        ),
    )


# ----------------------------
# Contract/Order builders
# ----------------------------
def _normalize_exchange(sec_type: str, symbol: str, exchange: str) -> str:
    sec = _u(sec_type)
    sym = _u(symbol)
    exch = (exchange or "").strip().upper()
    if sec == "FUT" and not exch and sym in {"MHG", "HG"}:
        return "COMEX"
    return exch


def _build_contract_from_item(it: Dict[str, Any]) -> Contract:
    c = Contract()
    c.conId = int(it.get("conId") or 0)
    c.symbol = str(it.get("symbol") or "")
    c.secType = str(it.get("secType") or "")
    c.currency = str(it.get("currency") or "USD")

    sec = _u(c.secType)
    exch = _normalize_exchange(c.secType, c.symbol, str(it.get("exchange") or ""))

    if sec == "STK":
        c.exchange = "SMART"
    else:
        c.exchange = exch or "SMART"

    ltd = str(it.get("lastTradeDateOrContractMonth") or "")
    if sec == "FUT" and ltd:
        c.lastTradeDateOrContractMonth = ltd

    c.localSymbol = str(it.get("localSymbol") or "")
    return c


def _build_mkt_order(action: str, qty: float) -> Order:
    o = Order()
    o.action = _u(action)
    o.orderType = "MKT"
    o.totalQuantity = float(qty or 0.0)
    o.tif = "DAY"
    # P1.3: sanitize deprecated attrs (prevents 10268/10270)
    try:
        sanitize_order_v0(o)
    except Exception:
        pass
    return o


def _safe_cancel_order(app: Any, order_id: int) -> None:
    try:
        app.cancelOrder(order_id)
    except TypeError:
        # some ibapi versions accept 2 args; fallback noop here
        app.cancelOrder(order_id, "")


def _classify_order_errors(
    errs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Split IBKR error callback records for a specific orderId into:
      - warnings (e.g., 399)
      - errors (everything else except INFO codes)
    """
    warns: List[Dict[str, Any]] = []
    fails: List[Dict[str, Any]] = []
    for e in errs:
        code = int(e.get("code") or 0)
        if code in INFO_CODES:
            continue
        if code in WARN_CODES:
            warns.append(e)
        else:
            fails.append(e)
    return warns, fails


class _App(EWrapper, EClient):
    """
    Minimal wrapper for P1.3:
      - capture nextValidId
      - capture per-order error/status/openOrder
      - allow waiting for first callback for a placed order
    """

    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._next_valid_id_evt = threading.Event()
        self._cv = threading.Condition()

        self.next_order_id: Optional[int] = None

        # system/info errors (reqId<=0)
        self.sys_msgs: List[str] = []

        # per orderId
        self.order_errors: Dict[int, List[Dict[str, Any]]] = {}
        self.order_status: Dict[int, Dict[str, Any]] = {}
        self.open_orders: Dict[int, Dict[str, Any]] = {}

    def nextValidId(self, orderId: int) -> None:
        self.next_order_id = int(orderId)
        self._next_valid_id_evt.set()
        with self._cv:
            self._cv.notify_all()

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        rec: Dict[str, Any] = {
            "reqId": int(reqId),
            "code": int(errorCode),
            "msg": str(errorString),
        }
        if advancedOrderRejectJson:
            rec["adv"] = advancedOrderRejectJson

        with self._cv:
            if int(reqId) > 0:
                self.order_errors.setdefault(int(reqId), []).append(rec)
            else:
                self.sys_msgs.append(
                    f"reqId={reqId} code={errorCode} msg={errorString}"
                )
            self._cv.notify_all()

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._cv:
            self.order_status[int(orderId)] = {
                "orderId": int(orderId),
                "status": str(status),
                "filled": float(filled) if filled is not None else None,
                "remaining": float(remaining) if remaining is not None else None,
                "avgFillPrice": avgFillPrice,
                "permId": int(permId),
                "clientId": int(clientId),
                "whyHeld": str(whyHeld or ""),
            }
            self._cv.notify_all()

    def openOrder(self, orderId, contract, order, orderState) -> None:
        with self._cv:
            self.open_orders[int(orderId)] = {
                "orderId": int(orderId),
                "permId": int(getattr(order, "permId", 0) or 0),
                "clientId": int(getattr(order, "clientId", 0) or 0),
                "account": str(getattr(order, "account", "") or ""),
                "symbol": str(getattr(contract, "symbol", "") or ""),
                "secType": str(getattr(contract, "secType", "") or ""),
                "conId": int(getattr(contract, "conId", 0) or 0),
                "exchange": str(getattr(contract, "exchange", "") or ""),
                "currency": str(getattr(contract, "currency", "") or ""),
                "action": str(getattr(order, "action", "") or ""),
                "orderType": str(getattr(order, "orderType", "") or ""),
                "tif": str(getattr(order, "tif", "") or ""),
                "totalQuantity": float(getattr(order, "totalQuantity", 0.0) or 0.0),
                "status": str(getattr(orderState, "status", "") or ""),
            }
            self._cv.notify_all()

    def wait_first_signal(self, order_id: int, timeout_s: float) -> Dict[str, Any]:
        """
        Wait until we get at least one of:
          - orderStatus for order_id
          - openOrder for order_id
          - error callback for order_id
        """
        end = time.time() + float(timeout_s)
        with self._cv:
            while time.time() < end:
                if order_id in self.order_status:
                    break
                if order_id in self.open_orders:
                    break
                if order_id in self.order_errors:
                    break
                remain = end - time.time()
                if remain <= 0:
                    break
                self._cv.wait(timeout=remain)

            return {
                "orderStatus": self.order_status.get(order_id),
                "openOrder": self.open_orders.get(order_id),
                "errors": self.order_errors.get(order_id, []),
            }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client-id", type=int, required=True)

    ap.add_argument("--control-plane", default=str(DATA_DIR / "control_plane.json"))
    ap.add_argument("--plan", default=str(DATA_DIR / "reset_plan.json"))
    ap.add_argument("--ledger", default=str(DATA_DIR / "reset_ledger.json"))
    ap.add_argument("--events", default=str(DATA_DIR / "reset_exec_events.jsonl"))

    ap.add_argument("--connect-timeout-s", type=float, default=8.0)
    ap.add_argument("--confirm-paper", action="store_true")

    ap.add_argument(
        "--listen-after-place-s",
        type=float,
        default=2.5,
        help="P1.3: wait for callbacks after placeOrder",
    )
    ap.add_argument("--out", default=str(DATA_DIR / "reset_exec_report_v1.json"))
    ap.add_argument(
        "--preview-out", default=str(DATA_DIR / "reset_exec_preview_v1.json")
    )

    args = ap.parse_args(argv)
    ts = _utc_now_iso()
    lock_path = LOGS_DIR / "reset_executor_paper_v1.lock"

    events_path = Path(args.events)
    ledger_path = Path(args.ledger)

    try:
        _lock_or_fail(lock_path)

        if not STOP_FLAG.exists():
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": f"STOP_FLAG_REQUIRED: create {str(STOP_FLAG)} before execution",
                "executed": False,
            }
            _write_json(Path(args.out), out)
            append_event(events_path, "RESET_BLOCKED", {"reason": "STOP_FLAG_REQUIRED"})
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        cp = _read_json(Path(args.control_plane))
        plan = _read_json(Path(args.plan))

        execution_mode = _u(cp.get("execution_mode"))
        global_mode = _u(cp.get("global_mode"))
        enable_paper = bool(cp.get("enable_paper_execution", False))

        items = list(plan.get("items") or [])
        plan_hash = str(plan.get("plan_hash") or "")

        ledger = load_ledger(ledger_path)

        manual_cancel_required = [
            it for it in items if it.get("kind") == "MANUAL_CANCEL_REQUIRED"
        ]
        cancel_items = [
            it
            for it in items
            if it.get("kind") == "CANCEL_ORDER" and int(it.get("orderId") or 0) > 0
        ]
        close_items = [it for it in items if it.get("kind") == "CLOSE_POSITION"]

        preview = {
            "schema": "reset_exec_preview_v1",
            "ts_utc": ts,
            "plan_hash": plan_hash,
            "control_plane": {
                "global_mode": global_mode,
                "execution_mode": execution_mode,
                "enable_paper_execution": enable_paper,
                "confirm_paper_flag": bool(args.confirm_paper),
                "listen_after_place_s": float(args.listen_after_place_s),
            },
            "summary": {
                "items_total": len(items),
                "manual_cancel_required": len(manual_cancel_required),
                "cancel_orders_executable": len(cancel_items),
                "close_positions": len(close_items),
            },
            "manual_cancel_required": manual_cancel_required,
            "cancel_orders_executable": cancel_items,
            "close_positions": close_items,
            "notes": [
                "MANUAL_CANCEL_REQUIRED is non-executable via API (orderId<=0).",
                "Ledger is monotonic: states never downgrade (SENT will not be overwritten by PLANNED).",
                "Events are appended to reset_exec_events.jsonl (forensics/replay).",
                "P1.3: sanitize deprecated order attrs + listen-after-place for callbacks.",
            ],
        }
        _write_json(Path(args.preview_out), preview)

        # Ledger PLANNED (monotonic, won't downgrade)
        planned_keys: List[str] = []
        already_sent: int = 0
        for it in items:
            k = build_action_key(it, plan_hash)
            planned_keys.append(k)
            prev = get_entry(ledger, k)
            if prev and _u(prev.get("state")) in {
                "SENT",
                "ACK",
                "FILLED",
                "REJECTED",
                "CANCELLED",
            }:
                already_sent += 1
                continue

            _ledger_upsert_monotonic(
                ledger,
                key=k,
                kind=str(it.get("kind") or ""),
                state="PLANNED",
                ts_utc=ts,
                details={
                    "plan_hash": plan_hash,
                    "symbol": it.get("symbol"),
                    "conId": int(it.get("conId") or 0),
                    "orderId": int(it.get("orderId") or 0),
                    "permId": int(it.get("permId") or 0),
                    "close_action": it.get("close_action"),
                    "qty": it.get("qty"),
                },
            )

        save_ledger(ledger_path, ledger)
        append_event(
            events_path,
            "RESET_PLANNED",
            {"plan_hash": plan_hash, "items_total": len(items)},
        )

        # Hard block by default
        can_execute = (
            (execution_mode == "PAPER") and enable_paper and bool(args.confirm_paper)
        )
        if not can_execute:
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": True,
                "exit_code": 0,
                "executed": False,
                "block_reason": "PAPER_EXECUTION_NOT_ENABLED",
                "written": {
                    "preview": str(Path(args.preview_out)),
                    "ledger": str(ledger_path),
                    "events": str(events_path),
                },
                "ledger_planned_keys_count": len(planned_keys),
                "ledger_already_sent_count": already_sent,
            }
            _write_json(Path(args.out), out)
            append_event(
                events_path,
                "RESET_BLOCKED",
                {"reason": "PAPER_EXECUTION_NOT_ENABLED", "plan_hash": plan_hash},
            )
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 0

        # ONLY_EXITS required for reset
        if global_mode and global_mode != "ONLY_EXITS":
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "executed": False,
                "error": f"global_mode_not_only_exits: {global_mode}",
                "written": {
                    "preview": str(Path(args.preview_out)),
                    "ledger": str(ledger_path),
                    "events": str(events_path),
                },
            }
            _write_json(Path(args.out), out)
            append_event(
                events_path,
                "RESET_BLOCKED",
                {"reason": "GLOBAL_MODE_NOT_ONLY_EXITS", "plan_hash": plan_hash},
            )
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        # Manual blockers -> BLOCKED and stop
        if len(manual_cancel_required) > 0:
            for it in manual_cancel_required:
                k = build_action_key(it, plan_hash)
                _ledger_upsert_monotonic(
                    ledger,
                    key=k,
                    kind=str(it.get("kind") or ""),
                    state="BLOCKED",
                    ts_utc=ts,
                    details={
                        "reason": "MANUAL_CANCEL_REQUIRED_PRESENT",
                        "plan_hash": plan_hash,
                    },
                )
            save_ledger(ledger_path, ledger)

            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 1,
                "executed": False,
                "error": "MANUAL_CANCEL_REQUIRED_PRESENT: cannot proceed until manual orders are cleared or filled",
                "manual_cancel_required": manual_cancel_required,
                "written": {
                    "preview": str(Path(args.preview_out)),
                    "ledger": str(ledger_path),
                    "events": str(events_path),
                },
            }
            _write_json(Path(args.out), out)
            append_event(
                events_path,
                "RESET_BLOCKED",
                {"reason": "MANUAL_CANCEL_REQUIRED_PRESENT", "plan_hash": plan_hash},
            )
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 1

        # REAL execution path
        append_event(events_path, "RESET_EXECUTE_START", {"plan_hash": plan_hash})

        app = _App()
        try:
            app.connect(args.host, int(args.port), int(args.client_id))
        except Exception as e:
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": f"connect_failed: {type(e).__name__}: {e}",
                "executed": False,
                "written": {
                    "preview": str(Path(args.preview_out)),
                    "ledger": str(ledger_path),
                    "events": str(events_path),
                },
            }
            _write_json(Path(args.out), out)
            append_event(
                events_path,
                "RESET_EXECUTE_FAIL",
                {"reason": "CONNECT_FAILED", "plan_hash": plan_hash},
            )
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        th = threading.Thread(target=app.run, daemon=True)
        th.start()

        if not app._next_valid_id_evt.wait(timeout=float(args.connect_timeout_s)):
            try:
                app.disconnect()
            except Exception:
                pass
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 2,
                "error": "handshake_timeout: no nextValidId",
                "executed": False,
                "written": {
                    "preview": str(Path(args.preview_out)),
                    "ledger": str(ledger_path),
                    "events": str(events_path),
                },
            }
            _write_json(Path(args.out), out)
            append_event(
                events_path,
                "RESET_EXECUTE_FAIL",
                {"reason": "HANDSHAKE_TIMEOUT", "plan_hash": plan_hash},
            )
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            return 2

        next_oid = int(app.next_order_id or 0)
        actions: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        warnings_all: List[Dict[str, Any]] = []

        # Phase 0: cancel executable orders (idempotent)
        for it in cancel_items:
            k = build_action_key(it, plan_hash)
            prev = get_entry(ledger, k)
            if prev and _u(prev.get("state")) in {"SENT", "ACK", "CANCELLED"}:
                actions.append(
                    {
                        "kind": "CANCEL_ORDER",
                        "skipped": True,
                        "reason": "IDEMPOTENT_ALREADY_SENT",
                        "key": k,
                    }
                )
                append_event(
                    events_path,
                    "ORDER_SKIP",
                    {"kind": "CANCEL_ORDER", "reason": "ALREADY_SENT", "key": k},
                )
                continue

            oid = int(it.get("orderId") or 0)
            if oid <= 0:
                _ledger_upsert_monotonic(
                    ledger,
                    key=k,
                    kind="CANCEL_ORDER",
                    state="SKIPPED",
                    ts_utc=ts,
                    details={"reason": "INVALID_ORDER_ID", "plan_hash": plan_hash},
                )
                actions.append(
                    {
                        "kind": "CANCEL_ORDER",
                        "skipped": True,
                        "reason": "INVALID_ORDER_ID",
                        "key": k,
                    }
                )
                append_event(
                    events_path,
                    "ORDER_SKIP",
                    {"kind": "CANCEL_ORDER", "reason": "INVALID_ORDER_ID", "key": k},
                )
                continue

            _safe_cancel_order(app, oid)

            _ledger_upsert_monotonic(
                ledger,
                key=k,
                kind="CANCEL_ORDER",
                state="SENT",
                ts_utc=ts,
                details={"orderId": oid, "plan_hash": plan_hash},
            )
            actions.append(
                {
                    "kind": "CANCEL_ORDER",
                    "orderId": oid,
                    "symbol": it.get("symbol"),
                    "permId": int(it.get("permId") or 0),
                    "key": k,
                }
            )
            append_event(
                events_path,
                "ORDER_SENT",
                {"kind": "CANCEL_ORDER", "orderId": oid, "key": k},
            )
            time.sleep(0.15)

        # Phase 1: close positions (idempotent) + P1.3 callbacks
        for it in close_items:
            k = build_action_key(it, plan_hash)
            prev = get_entry(ledger, k)
            if prev and _u(prev.get("state")) in {"SENT", "ACK", "FILLED"}:
                actions.append(
                    {
                        "kind": "CLOSE_POSITION",
                        "skipped": True,
                        "reason": "IDEMPOTENT_ALREADY_SENT",
                        "key": k,
                    }
                )
                append_event(
                    events_path,
                    "ORDER_SKIP",
                    {"kind": "CLOSE_POSITION", "reason": "ALREADY_SENT", "key": k},
                )
                continue

            qty = float(it.get("qty") or 0.0)
            if qty <= 0:
                _ledger_upsert_monotonic(
                    ledger,
                    key=k,
                    kind="CLOSE_POSITION",
                    state="SKIPPED",
                    ts_utc=ts,
                    details={"reason": "ZERO_QTY", "plan_hash": plan_hash},
                )
                actions.append(
                    {
                        "kind": "CLOSE_POSITION",
                        "skipped": True,
                        "reason": "ZERO_QTY",
                        "key": k,
                    }
                )
                append_event(
                    events_path,
                    "ORDER_SKIP",
                    {"kind": "CLOSE_POSITION", "reason": "ZERO_QTY", "key": k},
                )
                continue

            contract = _build_contract_from_item(it)
            order = _build_mkt_order(str(it.get("close_action") or ""), qty)

            # P1.3: sanitize again (defensive)
            try:
                san = sanitize_order_v0(order)
            except Exception:
                san = {}

            oid = next_oid
            next_oid += 1

            app.placeOrder(oid, contract, order)
            append_event(
                events_path,
                "ORDER_SENT",
                {
                    "kind": "CLOSE_POSITION",
                    "orderId": oid,
                    "action": order.action,
                    "qty": qty,
                    "key": k,
                    "sanitized": san,
                },
            )

            # P1.3: listen for callbacks (openOrder/orderStatus/error)
            snap = app.wait_first_signal(
                oid, timeout_s=float(args.listen_after_place_s)
            )
            errs = snap.get("errors") or []
            warns, fails = _classify_order_errors(errs)

            status = snap.get("orderStatus")
            openo = snap.get("openOrder")
            perm_id = 0
            if status and int(status.get("permId") or 0) > 0:
                perm_id = int(status.get("permId") or 0)
            elif openo and int(openo.get("permId") or 0) > 0:
                perm_id = int(openo.get("permId") or 0)

            # update events
            if warns:
                warnings_all.extend(warns)
                append_event(
                    events_path,
                    "ORDER_WARNING",
                    {"orderId": oid, "key": k, "warnings": warns},
                )
            if fails:
                failures.extend(fails)
                append_event(
                    events_path,
                    "ORDER_ERROR",
                    {"orderId": oid, "key": k, "errors": fails},
                )
            if openo:
                append_event(
                    events_path,
                    "OPEN_ORDER",
                    {"orderId": oid, "key": k, "openOrder": openo},
                )
            if status:
                append_event(
                    events_path,
                    "ORDER_STATUS",
                    {"orderId": oid, "key": k, "orderStatus": status},
                )

            # ledger state
            if fails:
                state = "REJECTED"
            elif status or openo:
                state = "ACK"
            else:
                state = "SENT"

            _ledger_upsert_monotonic(
                ledger,
                key=k,
                kind="CLOSE_POSITION",
                state=state,
                ts_utc=ts,
                details={
                    "orderId": oid,
                    "permId": perm_id,
                    "action": order.action,
                    "qty": qty,
                    "sanitized": san,
                    "orderStatus": status,
                    "openOrder": openo,
                    "warnings": warns,
                    "errors": fails,
                    "plan_hash": plan_hash,
                },
            )

            actions.append(
                {
                    "kind": "CLOSE_POSITION",
                    "orderId": oid,
                    "permId": perm_id,
                    "symbol": it.get("symbol"),
                    "qty": qty,
                    "action": order.action,
                    "state": state,
                    "key": k,
                }
            )

            time.sleep(0.2)

        try:
            app.disconnect()
        except Exception:
            pass

        save_ledger(ledger_path, ledger)
        append_event(
            events_path,
            "RESET_EXECUTE_DONE",
            {"plan_hash": plan_hash, "actions": len(actions)},
        )

        ok = True
        exit_code = 0
        # P1.3: treat non-warning per-order errors as eval fail
        if len(failures) > 0:
            ok = False
            exit_code = 1

        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": ok,
            "exit_code": exit_code,
            "executed": True,
            "actions": actions,
            "order_warnings": warnings_all[:50],
            "order_failures": failures[:50],
            "sys_msgs": app.sys_msgs[:20],
            "written": {
                "preview": str(Path(args.preview_out)),
                "ledger": str(ledger_path),
                "events": str(events_path),
            },
            "notes": [
                "P1.3: sanitize_order_v0 applied before placeOrder; nbboPriceCap removed if present in __dict__.",
                "P1.3: listen-after-place captures openOrder/orderStatus/errors; 399 treated as WARNING, not FAIL.",
                "Run reset_verify_v1 after execution; baseline requires positions=0 and open_orders=0.",
                "Ledger is monotonic and provides idempotency: SENT/ACK entries are not re-sent.",
            ],
        }

        _write_json(Path(args.out), out)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return exit_code

    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": _utc_now_iso(),
            "ok": False,
            "exit_code": 2,
            "error": f"{type(e).__name__}: {e}",
            "executed": False,
        }
        try:
            _write_json(Path(args.out), out)
        except Exception:
            pass
        append_event(
            Path(args.events),
            "RESET_EXECUTE_FAIL",
            {"reason": "EXCEPTION", "error": str(e)},
        )
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 2
    finally:
        _unlock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
