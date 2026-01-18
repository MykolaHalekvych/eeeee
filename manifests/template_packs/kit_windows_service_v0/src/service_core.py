from __future__ import annotations

from typing import Any, Dict


def service_status() -> Dict[str, Any]:
    # deterministic: no time, no network
    return {"ok": True, "state": "READY"}


def deterministic_tick() -> Dict[str, Any]:
    return {"ok": True, "tick": 1}
