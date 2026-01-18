# args/ibkr/ibkr_preflight_v0.py
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper


@dataclass(frozen=True)
class IbkrEndpoint:
    host: str
    port: int
    client_id: int


class IbkrPreflightError(RuntimeError):
    pass


class _PreflightApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._evt = threading.Event()
        self.next_valid_id: Optional[int] = None
        self.errors: List[Dict[str, Any]] = []
        self._t0 = time.time()

    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id = int(orderId)
        self._evt.set()

    # IB API sometimes calls error with 3 or 4 args; keep compatible
    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        self.errors.append(
            {
                "ts": time.time(),
                "reqId": reqId,
                "errorCode": errorCode,
                "errorString": errorString,
            }
        )

    def wait_next_valid_id(self, timeout_s: float) -> int:
        ok = self._evt.wait(timeout_s)
        if ok and self.next_valid_id is not None:
            return self.next_valid_id
        raise TimeoutError("nextValidId not received")


def preflight_next_valid_id(
    ep: IbkrEndpoint,
    timeout_s: float = 10.0,
) -> int:
    app = _PreflightApp()

    try:
        app.connect(ep.host, ep.port, clientId=ep.client_id)
    except Exception as e:
        raise IbkrPreflightError(
            f"IBKR preflight connect() failed: host={ep.host} port={ep.port} client_id={ep.client_id} "
            f"error={type(e).__name__}:{e}"
        ) from e

    t = threading.Thread(target=app.run, name="ibkr_preflight_runloop", daemon=True)
    t.start()

    try:
        nxt = app.wait_next_valid_id(timeout_s)
        return nxt
    except TimeoutError:
        # include last few errors if any
        tail = app.errors[-5:]
        hints = (
            "Hints: verify TWS/IB Gateway is running & logged in; API enabled (Enable ActiveX and Socket Clients); "
            "correct port (TWS live 7496 / paper 7497; Gateway live 4001 / paper 4002); "
            "firewall; client_id uniqueness; Trusted IPs."
        )
        raise IbkrPreflightError(
            f"IBKR preflight timeout waiting nextValidId (timeout_s={timeout_s}). "
            f"endpoint=host={ep.host} port={ep.port} client_id={ep.client_id}. "
            f"errors_tail={tail}. {hints}"
        )
    finally:
        try:
            app.disconnect()
        except Exception:
            pass
