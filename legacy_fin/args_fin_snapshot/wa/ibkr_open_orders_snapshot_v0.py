# args/wa/ibkr_open_orders_snapshot_v0.py
from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = "ibkr_open_orders_snapshot_v0"
SOURCE = "IBKR_OPEN_ORDERS_SNAPSHOT_V0"

# IBKR informational connectivity messages (NOT real errors for reconciliation)
_INFO_CODES = {2104, 2106, 2158}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _b01(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _json_compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(_json_compact(obj))
    f.write("\n")


def _safe_num(x: Any) -> Any:
    if isinstance(x, (int, float, str)) or x is None:
        return x
    return str(x)


def _contract_to_dict(c: Any) -> Dict[str, Any]:
    keys = [
        "conId",
        "symbol",
        "localSymbol",
        "secType",
        "exchange",
        "primaryExchange",
        "currency",
        "multiplier",
        "tradingClass",
        "lastTradeDateOrContractMonth",
        "includeExpired",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        try:
            out[k] = _safe_num(getattr(c, k))
        except Exception:
            pass
    return out


def _order_to_dict(o: Any) -> Dict[str, Any]:
    keys = [
        "action",
        "totalQuantity",
        "orderType",
        "lmtPrice",
        "auxPrice",
        "tif",
        "transmit",
        "outsideRth",
        "account",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        try:
            out[k] = _safe_num(getattr(o, k))
        except Exception:
            pass
    return out


def _order_state_to_dict(s: Any) -> Dict[str, Any]:
    keys = [
        "status",
        "warningText",
        "completedTime",
        "completedStatus",
        "initMarginBefore",
        "maintMarginBefore",
        "equityWithLoanBefore",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        try:
            out[k] = _safe_num(getattr(s, k))
        except Exception:
            pass
    return out


@dataclass(frozen=True)
class IbkrConn:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    timeout_s: float = 15.0


class _SnapshotApp:
    """
    Connects to IBKR and requests open orders snapshot.
    Emits JSONL events:
      - IBKR_SNAPSHOT_START / IBKR_SNAPSHOT_END
      - IBKR_OPEN_ORDER / IBKR_OPEN_ORDER_END
      - IBKR_ORDER_STATUS
      - IBKR_INFO (informational IBKR codes 2104/2106/2158)
      - IBKR_ERROR (real errors)
    """

    def __init__(
        self, conn: IbkrConn, *, tag: str, out_path: Path, wait_after_end_s: float
    ) -> None:
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        self._conn = conn
        self._tag = tag
        self._out_path = out_path
        self._wait_after_end_s = float(wait_after_end_s)

        self._lock = threading.Lock()
        self._open_orders = 0
        self._status_events = 0
        self._errors = 0
        self._infos = 0

        self._open_end_ev = threading.Event()
        self._next_id_ev = threading.Event()
        self._next_id: Optional[int] = None

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_SnapshotApp"):
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                self._outer._on_next_valid_id(orderId)

            def openOrder(self, orderId, contract, order, orderState) -> None:  # noqa: N802
                self._outer._on_open_order(orderId, contract, order, orderState)

            def openOrderEnd(self) -> None:  # noqa: N802
                self._outer._on_open_order_end()

            def orderStatus(  # noqa: N802
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
                self._outer._on_order_status(
                    orderId=orderId,
                    status=status,
                    filled=filled,
                    remaining=remaining,
                    avgFillPrice=avgFillPrice,
                    lastFillPrice=lastFillPrice,
                    whyHeld=whyHeld,
                    permId=permId,
                    parentId=parentId,
                    clientId=clientId,
                )

            def error(
                self, reqId, errorCode, errorString, advancedOrderRejectJson=""
            ) -> None:  # type: ignore  # noqa: N802
                self._outer._on_error(
                    reqId, errorCode, errorString, advancedOrderRejectJson
                )

        self._app = App(self)
        self._thread: Optional[threading.Thread] = None

    def connect(self) -> None:
        self._app.connect(
            self._conn.host, int(self._conn.port), int(self._conn.client_id)
        )
        self._thread = threading.Thread(target=self._app.run, daemon=True)
        self._thread.start()

        if not self._next_id_ev.wait(timeout=self._conn.timeout_s):
            raise RuntimeError("IBKR connect: timeout waiting for nextValidId()")

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def request_snapshot(self, *, all_open: bool) -> None:
        # Create/overwrite snapshot file
        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._out_path.open("w", encoding="utf-8") as f:
            _write_jsonl_line(
                f,
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "IBKR_SNAPSHOT_START",
                    "event_id": f"IBKR_SNAPSHOT_START:{self._tag}",
                    "ts": _now_utc_iso(),
                    "tag": self._tag,
                    "conn": {
                        "host": self._conn.host,
                        "port": self._conn.port,
                        "client_id": self._conn.client_id,
                        "timeout_s": self._conn.timeout_s,
                    },
                    "all_open": bool(all_open),
                    "source": SOURCE,
                },
            )

        if all_open:
            self._app.reqAllOpenOrders()
        else:
            self._app.reqOpenOrders()

        # Wait for end marker
        self._open_end_ev.wait(timeout=self._conn.timeout_s)

        # Give a small window for orderStatus callbacks to arrive
        if self._wait_after_end_s > 0:
            time.sleep(self._wait_after_end_s)

        with self._out_path.open("a", encoding="utf-8") as f:
            _write_jsonl_line(
                f,
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "IBKR_SNAPSHOT_END",
                    "event_id": f"IBKR_SNAPSHOT_END:{self._tag}",
                    "ts": _now_utc_iso(),
                    "tag": self._tag,
                    "counts": {
                        "open_orders": int(self._open_orders),
                        "status_events": int(self._status_events),
                        "infos": int(self._infos),
                        "errors": int(self._errors),
                    },
                    "source": SOURCE,
                },
            )

    def summary(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "tag": self._tag,
            "out_path": str(self._out_path),
            "counts": {
                "open_orders": int(self._open_orders),
                "status_events": int(self._status_events),
                "infos": int(self._infos),
                "errors": int(self._errors),
            },
        }

    # ---- callbacks -> JSONL ----

    def _append_event(self, ev: Dict[str, Any]) -> None:
        with self._out_path.open("a", encoding="utf-8") as f:
            _write_jsonl_line(f, ev)

    def _on_next_valid_id(self, order_id: int) -> None:
        self._next_id = int(order_id)
        self._next_id_ev.set()

    def _on_open_order(
        self, order_id: Any, contract: Any, order: Any, order_state: Any
    ) -> None:
        try:
            oid = int(order_id)
        except Exception:
            oid = -1

        ev = {
            "schema_version": SCHEMA_VERSION,
            "kind": "IBKR_OPEN_ORDER",
            "event_id": f"IBKR_OPEN_ORDER:{self._tag}:{oid}",
            "ts": _now_utc_iso(),
            "tag": self._tag,
            "order_id": oid,
            "contract": _contract_to_dict(contract),
            "order": _order_to_dict(order),
            "order_state": _order_state_to_dict(order_state),
            "source": SOURCE,
        }
        self._append_event(ev)
        with self._lock:
            self._open_orders += 1

    def _on_open_order_end(self) -> None:
        ev = {
            "schema_version": SCHEMA_VERSION,
            "kind": "IBKR_OPEN_ORDER_END",
            "event_id": f"IBKR_OPEN_ORDER_END:{self._tag}",
            "ts": _now_utc_iso(),
            "tag": self._tag,
            "source": SOURCE,
        }
        self._append_event(ev)
        self._open_end_ev.set()

    def _on_order_status(
        self,
        *,
        orderId: Any,
        status: Any,
        filled: Any,
        remaining: Any,
        avgFillPrice: Any,
        lastFillPrice: Any,
        whyHeld: Any,
        permId: Any,
        parentId: Any,
        clientId: Any,
    ) -> None:
        try:
            oid = int(orderId)
        except Exception:
            oid = -1

        st = str(status or "").strip().upper() or "UNKNOWN"
        ev_id = f"IBKR_ORDER_STATUS:{self._tag}:{oid}:{st}:{filled}:{remaining}"

        ev = {
            "schema_version": SCHEMA_VERSION,
            "kind": "IBKR_ORDER_STATUS",
            "event_id": ev_id,
            "ts": _now_utc_iso(),
            "tag": self._tag,
            "order_id": oid,
            "status": status,
            "filled": filled,
            "remaining": remaining,
            "avgFillPrice": avgFillPrice,
            "lastFillPrice": lastFillPrice,
            "whyHeld": whyHeld,
            "permId": permId,
            "parentId": parentId,
            "clientId": clientId,
            "source": SOURCE,
        }
        self._append_event(ev)
        with self._lock:
            self._status_events += 1

    def _on_error(
        self, req_id: Any, error_code: Any, error_str: Any, advanced: Any
    ) -> None:
        # classify info vs error
        try:
            code_i = int(error_code)
        except Exception:
            code_i = None

        is_info = code_i in _INFO_CODES

        ev = {
            "schema_version": SCHEMA_VERSION,
            "kind": ("IBKR_INFO" if is_info else "IBKR_ERROR"),
            "event_id": f"{'IBKR_INFO' if is_info else 'IBKR_ERROR'}:{self._tag}:{req_id}:{error_code}",
            "ts": _now_utc_iso(),
            "tag": self._tag,
            "req_id": req_id,
            "error_code": error_code,
            "error_string": str(error_str),
            "source": SOURCE,
        }
        if isinstance(advanced, str) and advanced.strip():
            ev["advanced_order_reject_json"] = advanced

        self._append_event(ev)

        with self._lock:
            if is_info:
                self._infos += 1
            else:
                self._errors += 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="ibkr_open_orders_snapshot_v0")

    ap.add_argument("--tag", default="", help="snapshot tag (default: auto)")
    ap.add_argument(
        "--out",
        default="",
        help="output jsonl (default: args/data/ibkr_open_orders_<tag>.jsonl)",
    )
    ap.add_argument(
        "--all-open", default="0", help="0/1: reqAllOpenOrders (else reqOpenOrders)"
    )

    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=11)
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument("--wait-after-end-s", type=float, default=2.0)

    args = ap.parse_args()
    repo_root = _repo_root()

    tag = str(args.tag or "").strip() or f"snapshot_{int(time.time())}"
    all_open = _b01(args.all_open)

    if str(args.out or "").strip():
        out_path = Path(str(args.out).strip())
        if not out_path.is_absolute():
            out_path = repo_root / out_path
    else:
        out_path = repo_root / "args" / "data" / f"ibkr_open_orders_{tag}.jsonl"

    conn = IbkrConn(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_s=float(args.timeout_s),
    )

    app = _SnapshotApp(
        conn, tag=tag, out_path=out_path, wait_after_end_s=float(args.wait_after_end_s)
    )
    app.connect()
    try:
        app.request_snapshot(all_open=all_open)
    finally:
        app.disconnect()

    print(json.dumps(app.summary(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
