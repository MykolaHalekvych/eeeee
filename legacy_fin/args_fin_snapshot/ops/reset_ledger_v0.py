# args/ops/reset_ledger_v0.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "reset_ledger_v0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _sha256_obj(obj: Dict[str, Any]) -> str:
    b = json.dumps(obj, sort_keys=True).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


@dataclass
class LedgerEntry:
    key: str
    kind: str  # MANUAL_CANCEL_REQUIRED / CANCEL_ORDER / CLOSE_POSITION
    state: str  # PLANNED / SENT / SKIPPED / BLOCKED
    ts_utc: str
    details: Dict[str, Any]


def build_action_key(item: Dict[str, Any], plan_hash: str) -> str:
    # Stable key: plan_hash + kind + contract identity + intended action
    kind = str(item.get("kind") or "")
    core = {
        "plan_hash": plan_hash,
        "kind": kind,
        "conId": int(item.get("conId") or 0),
        "symbol": str(item.get("symbol") or ""),
        "secType": str(item.get("secType") or ""),
        "localSymbol": str(item.get("localSymbol") or ""),
        "orderId": int(item.get("orderId") or 0),
        "permId": int(item.get("permId") or 0),
        "close_action": str(item.get("close_action") or ""),
        "qty": float(item.get("qty") or 0.0),
        "order_action": str(item.get("order_action") or ""),
        "order_type": str(item.get("order_type") or ""),
    }
    return _sha256_obj(core)


def load_ledger(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "schema": SCHEMA,
            "created_ts_utc": _utc_now_iso(),
            "updated_ts_utc": _utc_now_iso(),
            "entries": {},  # key -> LedgerEntry dict
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(path: Path, ledger: Dict[str, Any]) -> None:
    ledger["updated_ts_utc"] = _utc_now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_entry(ledger: Dict[str, Any], entry: LedgerEntry) -> None:
    if "entries" not in ledger or not isinstance(ledger["entries"], dict):
        ledger["entries"] = {}
    ledger["entries"][entry.key] = {
        "key": entry.key,
        "kind": entry.kind,
        "state": entry.state,
        "ts_utc": entry.ts_utc,
        "details": entry.details,
    }


def get_entry(ledger: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    ent = (ledger.get("entries") or {}).get(key)
    return ent if isinstance(ent, dict) else None
