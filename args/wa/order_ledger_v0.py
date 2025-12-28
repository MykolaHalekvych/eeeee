from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


SCHEMA_VERSION = "order_ledger_v0"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Keys that must not affect idempotency fingerprint
_VOLATILE_KEYS = {
    "run_id",
    "ts",
    "timestamp",
    "created_at",
    "updated_at",
    "event_id",
    "event_uid",
    "id",
    "line_no",
    "line_idx",
}


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def compute_idempotency_key_from_plan(plan: Dict[str, Any], *, prefix: str = "ibkr_place_order") -> Tuple[str, str]:
    """
    Returns:
      ledger_key: stable unique key used by ledger
      plan_fp: deterministic fingerprint of plan (for debug)
    Priority:
      1) plan['idempotency_key'] if present and non-empty
      2) deterministic fingerprint from plan with volatile keys stripped
    """
    v = plan.get("idempotency_key")
    if isinstance(v, str) and v.strip():
        raw = v.strip()
        fp = _sha256_hex(raw)
        return f"{prefix}:{raw}", fp

    stripped = _strip_volatile(plan)
    fp = _sha256_hex(_canonical(stripped))
    return f"{prefix}:{fp}", fp


_FINAL_STATUSES = {"ACK", "REJECT", "ERROR"}


@dataclass(frozen=True)
class LedgerState:
    ledger_key: str
    status: str
    updated_at: str
    record: Dict[str, Any]


class _LockFile:
    def __init__(self, lock_path: Path, timeout_s: float = 30.0, poll_s: float = 0.1) -> None:
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fd: Optional[int] = None

    def __enter__(self) -> "_LockFile":
        start = time.time()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                self._fd = fd
                os.write(fd, str(os.getpid()).encode("utf-8"))
                return self
            except FileExistsError:
                if (time.time() - start) > self.timeout_s:
                    raise TimeoutError(f"Ledger lock timeout: {self.lock_path}")
                time.sleep(self.poll_s)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        finally:
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except Exception:
                pass


class OrderLedgerV0:
    """
    JSONL ledger with a simple state machine:

      RESERVE  -> ACK/REJECT/ERROR (FINALIZE)

    Idempotency rule:
      - If ledger has RESERVED or FINAL status, we skip second submit.
      - allow_retry=0 by default: even after REJECT/ERROR, we do NOT resubmit.
    """

    def __init__(self, ledger_path: str | Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.lock_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock")

    def _latest_by_key(self) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for rec in iter_jsonl(self.ledger_path):
            k = rec.get("ledger_key")
            if isinstance(k, str) and k:
                latest[k] = rec
        return latest

    def get_state(self, ledger_key: str) -> Optional[LedgerState]:
        latest = None
        for rec in iter_jsonl(self.ledger_path):
            if rec.get("ledger_key") == ledger_key:
                latest = rec
        if not latest:
            return None
        status = str(latest.get("status", "")).upper()
        updated_at = str(latest.get("updated_at", "")) or str(latest.get("created_at", ""))
        return LedgerState(ledger_key=ledger_key, status=status, updated_at=updated_at, record=latest)

    def reserve(self, ledger_key: str, *, run_id: str, plan_meta: Dict[str, Any], allow_retry: bool = False) -> bool:
        with _LockFile(self.lock_path):
            latest = self._latest_by_key().get(ledger_key)
            if latest:
                status = str(latest.get("status", "")).upper()
                if status == "RESERVED":
                    return False
                if status in _FINAL_STATUSES and not allow_retry:
                    return False

            rec = {
                "schema_version": SCHEMA_VERSION,
                "ledger_kind": "ORDER_LEDGER",
                "op": "RESERVE",
                "ledger_key": ledger_key,
                "status": "RESERVED",
                "created_at": _now_utc_iso(),
                "updated_at": _now_utc_iso(),
                "run_id": run_id,
                "plan_meta": plan_meta,
            }
            append_jsonl(self.ledger_path, rec)
            return True

    def finalize(self, ledger_key: str, *, run_id: str, status: str, result: Dict[str, Any]) -> None:
        status_u = str(status).upper()
        if status_u not in _FINAL_STATUSES:
            raise ValueError(f"finalize() invalid status: {status}")

        with _LockFile(self.lock_path):
            rec = {
                "schema_version": SCHEMA_VERSION,
                "ledger_kind": "ORDER_LEDGER",
                "op": "FINALIZE",
                "ledger_key": ledger_key,
                "status": status_u,
                "created_at": _now_utc_iso(),
                "updated_at": _now_utc_iso(),
                "run_id": run_id,
                "result": result,
            }
            append_jsonl(self.ledger_path, rec)
