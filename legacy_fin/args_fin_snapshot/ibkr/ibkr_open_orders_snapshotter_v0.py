from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = "ibkr_open_orders_snapshotter_v0"
SNAPSHOT_SCHEMA_VERSION = "ibkr_open_orders_snapshot_v0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(_json_compact(obj))
    f.write("\n")


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _obj_to_dict(obj: Any, fields: list[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in fields:
        v = _safe_getattr(obj, k, None)
        if v is None:
            continue
        # keep JSON friendly primitives only
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            # some ibapi fields can be empty strings/0; non-primitives ignore
            continue
    return out


def _contract_to_dict(contract: Any) -> Dict[str, Any]:
    # include everything needed for reconcile matching
    fields = [
        "conId",
        "localSymbol",
        "symbol",
        "secType",
        "exchange",
        "primaryExchange",
        "currency",
        "lastTradeDateOrContractMonth",
        "contractMonth",
        "tradingClass",
        "multiplier",
        "includeExpired",
        "minTick",
        "marketName",
        "longName",
        "timeZoneId",
        "validExchanges",
    ]
    return _obj_to_dict(contract, fields)


def _order_to_dict(order: Any) -> Dict[str, Any]:
    fields = [
        "action",
        "orderType",
        "tif",
        "totalQuantity",
        "lmtPrice",
        "auxPrice",
        "transmit",
        "outsideRth",
        "account",
        "orderRef",
    ]
    return _obj_to_dict(order, fields)


def _order_state_to_dict(order_state: Any) -> Dict[str, Any]:
    fields = [
        "status",
        "initMarginBefore",
        "maintMarginBefore",
        "equityWithLoanBefore",
        "initMarginChange",
        "maintMarginChange",
        "equityWithLoanChange",
        "initMarginAfter",
        "maintMarginAfter",
        "equityWithLoanAfter",
        "commission",
        "minCommission",
        "maxCommission",
        "commissionCurrency",
        "warningText",
    ]
    return _obj_to_dict(order_state, fields)


@dataclass(frozen=True)
class IbkrConn:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    timeout_s: float = 15.0


class _SnapshotApp:
    """
    Minimal IBKR open-orders snapshotter:
      - connect
      - reqAllOpenOrders()
      - collect openOrder callbacks
      - wait for openOrderEnd
      - write JSONL snapshot
    """

    def __init__(self, conn: IbkrConn):
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

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

            def error(
                self, reqId, errorCode, errorString, advancedOrderRejectJson=""
            ) -> None:  # noqa: N802
                self._outer._on_error(
                    reqId, errorCode, errorString, advancedOrderRejectJson
                )

        self._conn = conn
        self._app = App(self)
        self._thread: Optional[threading.Thread] = None

        self._next_id_ev = threading.Event()
        self._end_ev = threading.Event()

        self._lock = threading.Lock()
        self._orders: list[Dict[str, Any]] = []
        self._errors: list[Dict[str, Any]] = []

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

    def _on_next_valid_id(self, order_id: int) -> None:
        self._next_id_ev.set()

    def _on_open_order(
        self, order_id: int, contract: Any, order: Any, order_state: Any
    ) -> None:
        evt = {
            "kind": "IBKR_OPEN_ORDER",
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "ts": _iso_utc_now(),
            "order_id": int(order_id) if isinstance(order_id, int) else order_id,
            "contract": _contract_to_dict(contract),
            "order": _order_to_dict(order),
            "order_state": _order_state_to_dict(order_state),
        }
        with self._lock:
            self._orders.append(evt)

    def _on_open_order_end(self) -> None:
        self._end_ev.set()

    def _on_error(
        self, req_id: Any, error_code: Any, error_str: Any, advanced: Any
    ) -> None:
        payload: Dict[str, Any] = {
            "kind": "IBKR_ERROR",
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "ts": _iso_utc_now(),
            "req_id": req_id,
            "error_code": error_code,
            "error_string": str(error_str),
        }
        if isinstance(advanced, str) and advanced.strip():
            payload["advanced_order_reject_json"] = advanced
        with self._lock:
            self._errors.append(payload)

    def snapshot_open_orders(self, *, wait_s: float) -> Dict[str, Any]:
        # clear state
        with self._lock:
            self._orders.clear()
            self._errors.clear()
        self._end_ev.clear()

        # request
        self._app.reqAllOpenOrders()

        # wait for openOrderEnd (or timeout)
        if not self._end_ev.wait(timeout=wait_s):
            return {
                "ok": False,
                "event": "timeout_waiting_openOrderEnd",
                "wait_s": wait_s,
            }

        with self._lock:
            orders = list(self._orders)
            errors = list(self._errors)

        return {"ok": True, "orders": orders, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(prog="ibkr_open_orders_snapshotter_v0")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=11)
    ap.add_argument("--timeout-s", type=float, default=15.0)
    ap.add_argument(
        "--wait-s",
        type=float,
        default=5.0,
        help="How long to wait for openOrderEnd after reqAllOpenOrders",
    )
    ap.add_argument("--out", default="args/data/ibkr_open_orders_live.jsonl")
    args = ap.parse_args()

    repo = _repo_root()
    out = Path(str(args.out))
    if not out.is_absolute():
        out = repo / out
    out.parent.mkdir(parents=True, exist_ok=True)

    conn = IbkrConn(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_s=float(args.timeout_s),
    )
    app = _SnapshotApp(conn)

    ts = _iso_utc_now()
    try:
        app.connect()
        res = app.snapshot_open_orders(wait_s=float(args.wait_s))
    finally:
        app.disconnect()

    # write snapshot file (always write start/end; fail-closed readers can decide)
    with out.open("w", encoding="utf-8") as f:
        _write_jsonl_line(
            f,
            {
                "kind": "IBKR_SNAPSHOT_START",
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "ts": ts,
                "source": SCHEMA_VERSION,
                "conn": {
                    "host": conn.host,
                    "port": conn.port,
                    "client_id": conn.client_id,
                    "timeout_s": conn.timeout_s,
                },
            },
        )

        if res.get("ok"):
            for evt in res.get("orders", []):
                if isinstance(evt, dict):
                    _write_jsonl_line(f, evt)
            # optional errors as separate records
            for evt in res.get("errors", []):
                if isinstance(evt, dict):
                    _write_jsonl_line(f, evt)
        else:
            _write_jsonl_line(
                f,
                {
                    "kind": "IBKR_SNAPSHOT_ERROR",
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "ts": _iso_utc_now(),
                    "details": dict(res),
                },
            )

        _write_jsonl_line(
            f,
            {
                "kind": "IBKR_SNAPSHOT_END",
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "ts": _iso_utc_now(),
            },
        )

    summary = {
        "ok": bool(res.get("ok")),
        "schema_version": SCHEMA_VERSION,
        "out_path": str(out),
        "orders": len(res.get("orders", []))
        if isinstance(res.get("orders"), list)
        else 0,
        "errors": len(res.get("errors", []))
        if isinstance(res.get("errors"), list)
        else 0,
        "result": {k: res.get(k) for k in ("ok", "event", "wait_s") if k in res},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
