# args/wa/order_sendplan_v0.py
from __future__ import annotations

import argparse
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


# -----------------------------
# Schema
# -----------------------------
SCHEMA_VERSION = "order_sendplan_v0"

_RX_PAYLOAD = re.compile(r"^orders_payload_(?P<rid>.+)\.jsonl$", re.IGNORECASE)


@dataclass(frozen=True)
class CliArgs:
    payload_path: Path
    out_path: Optional[Path]
    execute: bool
    max_lines: int


# -----------------------------
# IO helpers
# -----------------------------
def _resolve_path(p: str) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else (Path.cwd() / pp)


def _infer_run_id_from_filename(payload_path: Path) -> Optional[str]:
    m = _RX_PAYLOAD.match(payload_path.name)
    return m.group("rid") if m else None


def _read_jsonl(path: Path, *, max_lines: int) -> Tuple[int, int, Iterable[Dict[str, Any]]]:
    """
    Returns: (seen_lines, parse_errors, generator)
    """
    seen = 0
    errors = 0

    def gen():
        nonlocal seen, errors
        if not path.exists():
            return
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if seen >= max_lines:
                    break
                s = line.strip()
                if not s:
                    continue
                seen += 1
                try:
                    obj = json.loads(s)
                except Exception:
                    errors += 1
                    continue
                if isinstance(obj, dict):
                    yield obj

    return seen, errors, gen()


def _write_jsonl_atomic(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")
            n += 1

    tmp.replace(path)
    return n


# -----------------------------
# Core logic
# -----------------------------
def _payload_kind(obj: Dict[str, Any]) -> str:
    for k in ("payload_kind", "kind", "type"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return "UNKNOWN"


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    return []


def _mk_plan_from_payload(payload: Dict[str, Any], *, execute: bool) -> Dict[str, Any]:
    pk = _payload_kind(payload)

    mode = str(payload.get("mode") or "").strip().upper()
    gate_reason = payload.get("gate_reason")
    rule_ids = _as_list(payload.get("rule_ids"))
    env = payload.get("env")
    instrument = payload.get("instrument")
    timeframe = payload.get("timeframe")
    ts = payload.get("ts")
    idx = payload.get("index")
    run_id = payload.get("run_id")

    payload_id = payload.get("payload_id")
    payload_execute = bool(payload.get("execute")) if "execute" in payload else None
    intent_kind = payload.get("intent_kind")

    # Default plan
    plan_kind = "SENDPLAN_NONE"
    reason = str(payload.get("reason") or "").strip() or "payload_none"

    # If payload suggests a real broker order, map to a send action.
    # Safety: NO_TRADE dominates everything.
    if mode == "NO_TRADE":
        plan_kind = "SENDPLAN_NONE"
        if pk != "PAYLOAD_NONE":
            reason = f"mode_no_trade_overrides:{pk}"
    elif pk not in ("PAYLOAD_NONE", "UNKNOWN"):
        if "IBKR" in pk or "ORDER" in pk:
            plan_kind = "IBKR_PLACE_ORDER"
            reason = "payload_ibkr_order"
        else:
            plan_kind = "SENDPLAN_UNKNOWN"
            reason = f"unhandled_payload_kind:{pk}"

    plan: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "index": idx,
        "ts": ts,
        "timeframe": timeframe,
        "instrument": instrument,
        "env": env,
        "execute": bool(execute),

        # carry-through for audit/debug
        "mode": payload.get("mode"),
        "mode_source": payload.get("mode_source"),
        "gate_reason": gate_reason,
        "rule_ids": rule_ids,
        "intent_kind": intent_kind,
        "payload_kind": pk,
        "payload_id": payload_id,
        "payload_execute": payload_execute,

        # plan identity
        "plan_id": uuid.uuid4().hex[:16],
        "plan_kind": plan_kind,
        "reason": reason,
    }

    # Best-effort pass-through of broker payload (future-proof)
    # If your payload v0 later adds these fields, sendplan will carry them forward.
    for k in ("ibkr_order", "contract", "order", "order_id", "client_id", "account", "exchange", "currency"):
        if k in payload:
            plan[k] = payload.get(k)

    return plan


def build_sendplan(
    payload_path: Path,
    out_path: Path,
    *,
    execute: bool,
    max_lines: int,
) -> Dict[str, Any]:
    if not payload_path.exists():
        return {"ok": False, "error": f"payload file not found: {payload_path}", "payload_path": str(payload_path)}

    # Read payload
    seen0, err0, it = _read_jsonl(payload_path, max_lines=max_lines)

    # We need a stable run_id for out naming / sanity
    run_id = _infer_run_id_from_filename(payload_path)

    # Streaming plan generation
    counts = Counter()
    payload_total = 0

    def rows():
        nonlocal run_id, payload_total
        for obj in it:
            payload_total += 1
            if run_id is None:
                rid = obj.get("run_id")
                if isinstance(rid, str) and rid.strip():
                    run_id = rid.strip()

            plan = _mk_plan_from_payload(obj, execute=execute)
            k = str(plan.get("plan_kind") or "UNKNOWN").upper()
            counts[k] += 1
            yield plan

    # Write out
    written = _write_jsonl_atomic(out_path, rows())

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "execute": bool(execute),
        "payload_path": str(payload_path),
        "payload_total": int(payload_total),
        "payload_parse_seen": int(seen0),
        "payload_parse_errors": int(err0),
        "out_path": str(out_path),
        "plans_written": int(written),
        "plan_kinds": dict(counts),
        "run_id": run_id,
    }


# -----------------------------
# CLI
# -----------------------------
def _parse_args() -> CliArgs:
    ap = argparse.ArgumentParser(description="Build orders_sendplan_<run_id>.jsonl from orders_payload_<run_id>.jsonl (v0).")
    ap.add_argument("--payload", required=True, help="Path to orders_payload_<run_id>.jsonl (absolute or relative).")
    ap.add_argument("--out", default=None, help="Optional output path. Default: args/data/orders_sendplan_<run_id>.jsonl")
    ap.add_argument("--execute", type=int, default=0, help="0/1. Stored into output. (v0 does not place orders; it only builds sendplan.)")
    ap.add_argument("--max_lines", type=int, default=250_000, help="Safety bound for huge files.")
    a = ap.parse_args()

    payload_path = _resolve_path(a.payload)
    out_path = _resolve_path(a.out) if a.out else None
    execute = bool(int(a.execute))
    max_lines = int(a.max_lines)

    return CliArgs(payload_path=payload_path, out_path=out_path, execute=execute, max_lines=max_lines)


def main() -> int:
    a = _parse_args()

    # Default out path derived from run_id (prefer filename, fallback to "unknown")
    run_id = _infer_run_id_from_filename(a.payload_path) or "unknown"
    if a.out_path is None:
        data_dir = Path(__file__).resolve().parents[2] / "args" / "data"
        out_path = data_dir / f"orders_sendplan_{run_id}.jsonl"
    else:
        out_path = a.out_path

    res = build_sendplan(
        payload_path=a.payload_path,
        out_path=out_path,
        execute=a.execute,
        max_lines=a.max_lines,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
