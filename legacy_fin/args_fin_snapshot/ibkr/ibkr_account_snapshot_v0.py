# args/ibkr/ibkr_account_snapshot_v0.py
from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCHEMA_VERSION = "ibkr_account_snapshot_v0"


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
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
        self._lock = threading.Lock()

        self._rows: List[Dict[str, Any]] = []
        self._errors: List[Dict[str, Any]] = []

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_App"):
                EClient.__init__(self, self)
                self._o = outer

            def nextValidId(self, orderId: int) -> None:  # noqa: N802
                self._o._ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):  # noqa: N802
                self._o._errors.append(
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

            def accountSummary(self, reqId, account, tag, value, currency):  # noqa: N802
                with self._o._lock:
                    self._o._rows.append(
                        {
                            "account": str(account),
                            "tag": str(tag),
                            "value": str(value),
                            "currency": str(currency),
                        }
                    )

            def accountSummaryEnd(self, reqId):  # noqa: N802
                self._o._done.set()

        self._app = App(self)

    def run(self, *, tags: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        self._app.connect(self._ep.host, int(self._ep.port), int(self._ep.client_id))
        t = threading.Thread(target=self._app.run, daemon=True)
        t.start()

        if not self._ready.wait(timeout=self._ep.timeout_s):
            try:
                self._app.disconnect()
            except Exception:
                pass
            raise TimeoutError("Timeout waiting for nextValidId")

        req_id = 9001
        # groupName="All" is typical
        self._app.reqAccountSummary(req_id, "All", tags)

        self._done.wait(timeout=self._ep.timeout_s)
        try:
            self._app.cancelAccountSummary(req_id)
        except Exception:
            pass
        try:
            self._app.disconnect()
        except Exception:
            pass

        return list(self._rows), list(self._errors)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="IBKR account summary snapshot (read-only)."
    )
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--timeout-s", type=float, default=20.0)
    ap.add_argument("--run-id", default="", help="Optional run_id to name output file.")
    ap.add_argument(
        "--tags",
        default="NetLiquidation,AvailableFunds,InitMarginReq,MaintMarginReq,ExcessLiquidity,BuyingPower",
        help="Comma-separated AccountSummary tags.",
    )
    a = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    out_name = (
        f"ibkr_account_{a.run_id}.json"
        if str(a.run_id).strip()
        else f"ibkr_account_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path = repo_root / "args" / "data" / out_name

    ep = Endpoint(
        host=str(a.host),
        port=int(a.port),
        client_id=int(a.client_id),
        timeout_s=float(a.timeout_s),
    )
    rows, errors = _App(ep).run(tags=str(a.tags))

    obj = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _utc_now_z(),
        "endpoint": {"host": ep.host, "port": ep.port, "client_id": ep.client_id},
        "run_id": str(a.run_id).strip() or None,
        "tags": str(a.tags),
        "rows": rows,
        "errors": errors,
        "ok": True,
    }
    _atomic_write_json(out_path, obj)

    print(
        json.dumps(
            {
                "ok": True,
                "out_path": str(out_path),
                "rows": len(rows),
                "errors": len(errors),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
