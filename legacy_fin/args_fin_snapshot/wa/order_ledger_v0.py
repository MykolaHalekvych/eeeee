# args/wa/order_ledger_v0.py
from __future__ import annotations

import json
import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCHEMA = "order_ledger_v0"
LEDGER_SCHEMA_VERSION = "order_ledger_v0_jsonl"

# Monotonic state ranks (never downgrade)
_STATE_RANK = {
    "NEW": 0,
    "RESERVED": 10,
    "SUBMITTED": 20,
    "SENT": 25,
    "ACK": 30,
    "FILLED": 40,
    "CANCELLED": 40,
    "REJECT": 40,
    "REJECTED": 40,
    "ERROR": 40,
    "DONE": 50,
}

# Terminal states (dedupe should treat these as "final")
_TERMINAL = {"ACK", "FILLED", "CANCELLED", "REJECT", "REJECTED", "ERROR", "DONE"}

# Retry-eligible states (only if allow_retry=True)
_RETRY_ELIGIBLE = {"REJECT", "REJECTED", "ERROR"}

# Volatile keys to strip when hashing plans for stable fingerprint
_VOLATILE_KEYS = {
    "ts",
    "ts_utc",
    "timestamp",
    "created_at",
    "updated_at",
    "exec_id",
    "event_id",
    "order_id",
    "orderId",
    "permId",
    "ibkr_order_id",
    "ibkr_order_status",
}


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


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


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _rank(state: str) -> int:
    return _STATE_RANK.get(_u(state), 0)


def compute_idempotency_key_from_plan(
    plan: Dict[str, Any], *, prefix: str = ""
) -> Tuple[str, str]:
    """
    Returns (ledger_key, fingerprint).

    Priority:
      1) If plan has idempotency_key -> use it (namespaced by prefix + run_id)
      2) Else compute deterministic fingerprint from plan content (volatile keys stripped)
    """
    pfx = str(prefix or "").strip()
    if pfx:
        pfx = pfx.strip(":") + ":"

    run_id = str(plan.get("run_id") or "").strip()
    rid = run_id if run_id else "unknown"

    # Prefer explicit idempotency key if present
    ik = (
        plan.get("idempotency_key")
        or plan.get("ledger_key")
        or plan.get("idempotencyKey")
    )
    if isinstance(ik, str) and ik.strip():
        key = f"{pfx}{rid}:{ik.strip()}"
        return key, ""

    clean = _strip_volatile(plan)
    fp_full = _sha256_hex(_stable_json(clean))
    fp = fp_full[:16]
    key = f"{pfx}{rid}:{fp}"
    return key, fp_full


@dataclass(frozen=True)
class LedgerState:
    ledger_key: str
    state: str
    ts_utc: str
    run_id: str
    detail: Dict[str, Any]


class OrderLedgerV0:
    """
    JSONL-backed, monotonic ledger for idempotency + audit trail.

    Public API used by wa_ibkr_executor_v0:
      - reserve(ledger_key, run_id, plan_meta, allow_retry=False) -> bool
      - finalize(ledger_key, run_id, status, result) -> None
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._index: Dict[str, LedgerState] = {}
        self._load_best_effort()

    @property
    def path(self) -> Path:
        return self._path

    def _load_best_effort(self) -> None:
        """
        Build in-memory last-state index from JSONL.
        Fail-soft: ignore bad lines.
        """
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    lk = str(obj.get("ledger_key") or "").strip()
                    st = str(obj.get("state") or "").strip()
                    ts = str(obj.get("ts_utc") or obj.get("ts") or "").strip()
                    rid = str(obj.get("run_id") or "").strip()
                    if not lk or not st:
                        continue
                    detail = (
                        obj.get("detail") if isinstance(obj.get("detail"), dict) else {}
                    )
                    prev = self._index.get(lk)
                    # keep most advanced state by rank; if equal rank keep latest ts
                    if prev is None:
                        self._index[lk] = LedgerState(lk, st, ts, rid, dict(detail))
                    else:
                        if _rank(st) > _rank(prev.state):
                            self._index[lk] = LedgerState(lk, st, ts, rid, dict(detail))
                        elif _rank(st) == _rank(prev.state):
                            # prefer newer ts if comparable; otherwise keep existing
                            if ts and (not prev.ts_utc or ts >= prev.ts_utc):
                                self._index[lk] = LedgerState(
                                    lk, st, ts, rid, dict(detail)
                                )
        except Exception:
            # fail-soft
            return

    def _append(self, rec: Dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(_stable_json(rec))
            f.write("\n")

    def get_state(self, ledger_key: str) -> Optional[LedgerState]:
        with self._lock:
            return self._index.get(str(ledger_key or "").strip())

    def reserve(
        self,
        ledger_key: str,
        *,
        run_id: str,
        plan_meta: Optional[Dict[str, Any]] = None,
        allow_retry: bool = False,
    ) -> bool:
        """
        Reserve an idempotency key for this run.
        Returns True if reservation is accepted, False if deduped/blocked.

        Rules:
          - If key unseen -> reserve
          - If key terminal:
              - if allow_retry and state in {REJECT, REJECTED, ERROR} -> reserve_retry
              - else -> False
          - If key in-progress -> False
        """
        lk = str(ledger_key or "").strip()
        if not lk:
            return False

        rid = str(run_id or "").strip() or "unknown"
        ts = _utc_now_iso()

        with self._lock:
            prev = self._index.get(lk)
            prev_state = _u(prev.state) if prev else ""

            if prev is None:
                state = "RESERVED"
                rec = {
                    "schema": LEDGER_SCHEMA_VERSION,
                    "ts_utc": ts,
                    "kind": "LEDGER_RESERVE",
                    "ledger_key": lk,
                    "run_id": rid,
                    "state": state,
                    "detail": {"plan_meta": dict(plan_meta or {})},
                }
                self._append(rec)
                self._index[lk] = LedgerState(
                    lk, state, ts, rid, {"plan_meta": dict(plan_meta or {})}
                )
                return True

            # Terminal already?
            if prev_state in _TERMINAL:
                if allow_retry and prev_state in _RETRY_ELIGIBLE:
                    state = "RESERVED"
                    rec = {
                        "schema": LEDGER_SCHEMA_VERSION,
                        "ts_utc": ts,
                        "kind": "LEDGER_RESERVE_RETRY",
                        "ledger_key": lk,
                        "run_id": rid,
                        "state": state,
                        "detail": {
                            "retry_of": prev_state,
                            "prev_ts_utc": prev.ts_utc,
                            "plan_meta": dict(plan_meta or {}),
                        },
                    }
                    self._append(rec)
                    self._index[lk] = LedgerState(
                        lk,
                        state,
                        ts,
                        rid,
                        {
                            "retry_of": prev_state,
                            "prev_ts_utc": prev.ts_utc,
                            "plan_meta": dict(plan_meta or {}),
                        },
                    )
                    return True
                return False

            # In-progress -> dedupe
            return False

    def finalize(
        self,
        ledger_key: str,
        *,
        run_id: str,
        status: str,
        result: Dict[str, Any],
    ) -> None:
        """
        Finalize a ledger key with a (possibly terminal) status.
        Monotonic: never downgrade.
        """
        lk = str(ledger_key or "").strip()
        if not lk:
            return

        rid = str(run_id or "").strip() or "unknown"
        ts = _utc_now_iso()
        st = _u(status)

        # normalize common variants
        if st == "REJECTED":
            st = "REJECT"
        if st == "OK":
            st = "DONE"

        detail = {"result": dict(result or {})}

        with self._lock:
            prev = self._index.get(lk)
            prev_state = _u(prev.state) if prev else ""

            # Monotonic rule
            if prev is not None and _rank(prev_state) > _rank(st):
                # still append a note record for audit, but do not update index
                rec = {
                    "schema": LEDGER_SCHEMA_VERSION,
                    "ts_utc": ts,
                    "kind": "LEDGER_FINALIZE_IGNORED",
                    "ledger_key": lk,
                    "run_id": rid,
                    "state": st,
                    "detail": {
                        "prev_state": prev_state,
                        "prev_ts_utc": prev.ts_utc,
                        **detail,
                    },
                }
                self._append(rec)
                return

            rec = {
                "schema": LEDGER_SCHEMA_VERSION,
                "ts_utc": ts,
                "kind": "LEDGER_FINALIZE",
                "ledger_key": lk,
                "run_id": rid,
                "state": st,
                "detail": detail,
            }
            self._append(rec)
            self._index[lk] = LedgerState(lk, st, ts, rid, detail)

    # Optional helper (not required by current callers)
    def mark_progress(
        self,
        ledger_key: str,
        *,
        run_id: str,
        state: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Non-terminal progress marker (SENT/SUBMITTED/ACK etc).
        Monotonic.
        """
        lk = str(ledger_key or "").strip()
        if not lk:
            return
        rid = str(run_id or "").strip() or "unknown"
        ts = _utc_now_iso()
        st = _u(state)

        with self._lock:
            prev = self._index.get(lk)
            prev_state = _u(prev.state) if prev else ""

            if prev is not None and _rank(prev_state) > _rank(st):
                return

            rec = {
                "schema": LEDGER_SCHEMA_VERSION,
                "ts_utc": ts,
                "kind": "LEDGER_MARK",
                "ledger_key": lk,
                "run_id": rid,
                "state": st,
                "detail": dict(detail or {}),
            }
            self._append(rec)
            self._index[lk] = LedgerState(lk, st, ts, rid, dict(detail or {}))


# Optional CLI: quick inspect (JSON-only stdout)
def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog=SCHEMA)
    p.add_argument("--path", required=True)
    p.add_argument("--key", default="")
    args = p.parse_args(argv)

    path = Path(args.path)
    led = OrderLedgerV0(path)

    if args.key.strip():
        st = led.get_state(args.key.strip())
        out = {
            "schema": SCHEMA,
            "ts_utc": _utc_now_iso(),
            "ok": True,
            "exit_code": 0,
            "path": str(path),
            "key": args.key.strip(),
            "state": {
                "ledger_key": st.ledger_key,
                "state": st.state,
                "ts_utc": st.ts_utc,
                "run_id": st.run_id,
                "detail": st.detail,
            }
            if st
            else None,
        }
    else:
        out = {
            "schema": SCHEMA,
            "ts_utc": _utc_now_iso(),
            "ok": True,
            "exit_code": 0,
            "path": str(path),
            "keys": len(led._index),
        }

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
