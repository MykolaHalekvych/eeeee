"""
JSONL append-only event store for ARGS Core.

Functions:
- append_event(path: str, event: dict) -> None
- iter_events(path: str) -> Iterator[dict]

Rules:
- Append-only (no truncate / overwrite behavior)
- Deterministic JSON serialization for stable diffs and replay (sort_keys=True)
- Stdlib only
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterator


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _validate_event_schema(event: Dict[str, Any]) -> None:
    required = [
        "event_id",
        "ts_utc",
        "policy_name",
        "schema_version",
        "instrument",
        "timeframe",
        "environment",
        "ma_decision",
        "violations",
        "risk_envelope",
        "ctx_snapshot",
    ]
    missing = [k for k in required if k not in event]
    if missing:
        raise ValueError(f"Event missing required fields: {', '.join(missing)}")


def append_event(path: str, event: Dict[str, Any]) -> None:
    """
    Append one event as one JSON line.
    """
    _ensure_parent_dir(path)

    # Fill ts_utc if caller left it empty (demo convenience).
    if "ts_utc" not in event or not isinstance(event.get("ts_utc"), str) or not event.get("ts_utc"):
        event["ts_utc"] = _now_utc_iso()

    _validate_event_schema(event)

    line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.write("\n")


def iter_events(path: str) -> Iterator[Dict[str, Any]]:
    """
    Iterate events from JSONL file.
    """
    if not os.path.exists(path):
        return
        yield  # pragma: no cover

    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"Invalid event (not an object) at line {lineno}")
            yield obj
