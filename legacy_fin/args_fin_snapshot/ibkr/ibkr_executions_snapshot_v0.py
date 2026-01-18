# args/ibkr/ibkr_executions_snapshot_v0.py
from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCHEMA_VERSION = "ibkr_executions_snapshot_v0"


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    client_id: int
    timeout_s: float = 20.0


class _App:
    def __init__(self, ep: Endpoint):
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        self._ep = ep
        self._ready = threading.Event()
        self._done = threading.Event()

        self.rows: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_App"):
                EClient.__init__(self, self)
                self._o = outer

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                self._o._ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self._o.errors.append(
                    {
                        "ts": _utc_now_z(),
                        "reqId": reqId,
                        "code": int(errorCode)
                        if str(errorCode).lstrip("-").isdigit()
                        else errorCode,
                        "msg": str(errorString),
                        "advanced": str(advancedOrderRejectJson)
                        if isinstance(advancedOrderRejectJson, str)
                        else "",
                    }
                )

            def execDetails(self, reqId, contract, execution):  # noqa: N802
                self._o.rows.append(
                    {
                        "reqId": reqId,
                        "conId": getattr(contract, "conId", None),
                        "localSymbol": getattr(contract, "localSymbol", None),
                        "secType": getattr(contract, "secType", None),
                        "exchange": getattr(contract, "exchange", None),
                        "execId": getattr(execution, "execId", None),
                        "orderId": getattr(execution, "orderId", None),
                        "permId": getattr(execution, "permId", None),
                        "time": getattr(execution, "time", None),
                        "side": getattr(execution, "side", None),
                        "shares": getattr(execution, "shares", None),
                        "price": getattr(execution, "price", None),
                        "avgPrice": getattr(execution, "avgPrice", None),
                    }
                )

            def execDetailsEnd(self, reqId):  # noqa: N802
                self._o._done.set()

        self._app = App(self)

    def run(
        self, *, lookback_minutes: int
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        from ibapi.execution import ExecutionFilter  # type: ignore

        self._app.connect(self._ep.host, int(self._ep.port), int(self._ep.client_id))
        t = threading.Thread(target=self._app.run, daemon=True)
        t.start()

        if not self._ready.wait(timeout=self._ep.timeout_s):
            try:
                self._app.disconnect()
            except Exception:
                pass
            raise TimeoutError("Timeout waiting for nextValidId")

        flt = ExecutionFilter()
        # IB expects local/TWS time. We'll still provide UTC-like string; it generally works for filtering.
        if lookback_minutes > 0:
            dt = datetime.now(timezone.utc) - timedelta(minutes=int(lookback_minutes))
            flt.time = dt.strftime("%Y%m%d %H:%M:%S")

        req_id = 9002
        self._app.reqExecutions(req_id, flt)
        self._done.wait(timeout=self._ep.timeout_s)

        try:
            self._app.disconnect()
        except Exception:
            pass

        return list(self.rows), list(self.errors)


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR executions snapshot (read-only).")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--timeout-s", type=float, default=20.0)
    ap.add_argument("--run-id", default="")
    ap.add_argument(
        "--lookback-min",
        type=int,
        default=1440,
        help="Lookback minutes for executions filter.",
    )
    a = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    tag = str(a.run_id).strip() or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = repo_root / "args" / "data" / f"ibkr_executions_{tag}.jsonl"

    ep = Endpoint(
        host=str(a.host),
        port=int(a.port),
        client_id=int(a.client_id),
        timeout_s=float(a.timeout_s),
    )
    rows, errors = _App(ep).run(lookback_minutes=int(a.lookback_min))

    header = {
        "kind": "IBKR_EXECUTIONS_SNAPSHOT_START",
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_z(),
        "endpoint": {"host": ep.host, "port": ep.port, "client_id": ep.client_id},
        "run_id": str(a.run_id).strip() or None,
        "lookback_min": int(a.lookback_min),
    }
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    for r in rows:
        lines.append(
            json.dumps(
                {"kind": "IBKR_EXECUTION", **r}, ensure_ascii=False, sort_keys=True
            )
        )
    lines.append(
        json.dumps(
            {
                "kind": "IBKR_EXECUTIONS_SNAPSHOT_END",
                "ts_utc": _utc_now_z(),
                "count": len(rows),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    _atomic_write_text(out_path, "\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "out_path": str(out_path),
                "executions": len(rows),
                "errors": len(errors),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
