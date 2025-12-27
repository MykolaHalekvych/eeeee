from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCHEMA_VERSION = "ibkr_exec_ledger_v1"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


_VOLATILE_KEYS = {
    "ts", "timestamp", "created_at", "updated_at",
    "order_id", "orderId", "permId", "clientId",
    "run_id", "runId",
    "cursor", "last_used_order_id",
    "would_send", "decision",
}


def _strip_volatile(x: Any) -> Any:
    if isinstance(x, dict):
        out: Dict[str, Any] = {}
        for k, v in x.items():
            if str(k) in _VOLATILE_KEYS:
                continue
            out[str(k)] = _strip_volatile(v)
        return out
    if isinstance(x, list):
        return [_strip_volatile(v) for v in x]
    return x


def compute_fingerprint(sendplan_item: Dict[str, Any]) -> str:
    """
    Fingerprint must be stable across re-runs of the same sendplan.
    We drop volatile keys (timestamps/order_id/etc) and hash the remaining structure.
    """
    clean = _strip_volatile(sendplan_item)
    raw = _stable_json(clean).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def compute_send_key(run_id: str, sendplan_item: Dict[str, Any]) -> str:
    fp = compute_fingerprint(sendplan_item)
    return f"{run_id}:{fp}"


def default_ledger_path(repo_root: Path) -> Path:
    return repo_root / "args" / "data" / "ibkr_exec_ledger_v1.json"


@dataclass
class ExecLedger:
    path: Path
    data: Dict[str, Any]

    @property
    def sent(self) -> Dict[str, Any]:
        # schema: { "schema_version":..., "sent": { send_key: {...} } }
        sent = self.data.get("sent")
        if not isinstance(sent, dict):
            sent = {}
            self.data["sent"] = sent
        return sent

    def has(self, send_key: str) -> bool:
        return send_key in self.sent

    def mark_sent(
        self,
        send_key: str,
        *,
        order_id: Optional[int],
        mode: str,
        simulate: bool,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        rec = {
            "ts": _now_utc_iso(),
            "order_id": order_id,
            "mode": mode,
            "simulate": bool(simulate),
            "meta": meta or {},
        }
        self.sent[send_key] = rec

    def save_atomic(self) -> None:
        self.data["schema_version"] = SCHEMA_VERSION
        self.data.setdefault("created_at", _now_utc_iso())
        self.data["updated_at"] = _now_utc_iso()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(_stable_json(self.data) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def load_ledger(path: Path) -> ExecLedger:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # corrupted ledger => do not proceed silently; fail safe in caller
            raise
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("created_at", _now_utc_iso())
    data.setdefault("sent", {})
    return ExecLedger(path=path, data=data)
