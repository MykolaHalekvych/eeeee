# args/wa/order_sendplan_v0.py
from __future__ import annotations

import argparse
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple


SCHEMA_VERSION = "order_sendplan_v0"

_RX_PAYLOAD = re.compile(r"^orders_payload_(?P<rid>.+)\.jsonl$", re.IGNORECASE)


def _u16() -> str:
    return uuid.uuid4().hex[:16]


def _as_bool01(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(int(v))
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _payload_kind(obj: Dict[str, Any]) -> str:
    v = obj.get("payload_kind")
    return str(v or "").strip().upper() or "UNKNOWN"


def _infer_run_id_from_filename(payload_path: Path) -> str:
    m = _RX_PAYLOAD.match(payload_path.name)
    return m.group("rid") if m else "unknown"


def _iter_jsonl(path: Path, max_lines: int = 250_000) -> Tuple[int, int, Iterator[Dict[str, Any]]]:
    seen = 0
    errs = 0

    def gen() -> Iterator[Dict[str, Any]]:
        nonlocal seen, errs
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
                    errs += 1
                    continue
                if isinstance(obj, dict):
                    yield obj

    return seen, errs, gen()


def _write_jsonl_atomic(out_path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")
            n += 1
    tmp.replace(out_path)
    return n


def _mk_plan(payload: Dict[str, Any], *, execute: bool, force_one_plan: bool, forced_done: bool) -> Tuple[Dict[str, Any], bool]:
    """
    Returns (plan, forced_done_updated)
    """
    pk = _payload_kind(payload)

    plan: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": payload.get("run_id"),
        "index": payload.get("index"),
        "ts": payload.get("ts"),
        "instrument": payload.get("instrument"),
        "timeframe": payload.get("timeframe"),
        "env": payload.get("env"),
        "execute": bool(execute),  # controls executor attempt
        "payload_execute": bool(payload.get("execute", False)),
        "payload_id": payload.get("payload_id"),
        "payload_kind": pk,
        "intent_kind": payload.get("intent_kind"),
        "mode": payload.get("mode"),
        "mode_source": payload.get("mode_source"),
        "gate_reason": payload.get("gate_reason"),
        "rule_ids": payload.get("rule_ids") if isinstance(payload.get("rule_ids"), list) else [],
        "plan_id": _u16(),
        "plan_kind": "SENDPLAN_NONE",
        "reason": payload.get("reason") or "payload_none",
    }

    # Default: NONE
    if pk == "PAYLOAD_NONE" or pk == "" or pk == "UNKNOWN":
        return plan, forced_done

    # If payload contains ibkr blob, expose it in top-level for executor
    ibkr = payload.get("ibkr")
    if isinstance(ibkr, dict):
        c = ibkr.get("contract")
        o = ibkr.get("order")
        if isinstance(c, dict):
            plan["contract"] = c
        if isinstance(o, dict):
            plan["order"] = o

    # Normal mapping: IBKR order payload => actionable plan
    if pk in ("PAYLOAD_IBKR_ORDER", "IBKR_ORDER", "PAYLOAD_KIND_IBKR_ORDER"):
        plan["plan_kind"] = "PLAN_IBKR_PLACE_ORDER"
        plan["reason"] = payload.get("reason") or "payload_ibkr_order"

        # If force-one-plan is enabled, only the first actionable is kept; others downgraded
        if force_one_plan:
            if forced_done:
                plan["plan_kind"] = "SENDPLAN_NONE"
                plan["reason"] = "force_one_plan_only_first"
            else:
                forced_done = True

        return plan, forced_done

    # Anything else: unknown
    plan["plan_kind"] = "SENDPLAN_UNKNOWN"
    plan["reason"] = f"unhandled_payload_kind:{pk}"
    return plan, forced_done


def build_sendplan(payload_path: Path, out_path: Path, *, execute: bool, force_one_plan: bool, max_lines: int) -> Dict[str, Any]:
    if not payload_path.exists():
        return {"ok": False, "error": f"payload file not found: {payload_path}"}

    seen0, err0, it = _iter_jsonl(payload_path, max_lines=max_lines)

    counts = Counter()
    forced_done = False
    total = 0

    def rows() -> Iterator[Dict[str, Any]]:
        nonlocal forced_done, total
        for payload in it:
            total += 1
            plan, forced_done = _mk_plan(payload, execute=execute, force_one_plan=force_one_plan, forced_done=forced_done)
            counts[str(plan.get("plan_kind") or "UNKNOWN")] += 1
            yield plan

    written = _write_jsonl_atomic(out_path, rows())

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "execute": bool(execute),
        "force_one_plan": bool(force_one_plan),
        "payload_path": str(payload_path),
        "payload_total": int(total),
        "payload_parse_seen": int(seen0),
        "payload_parse_errors": int(err0),
        "out_path": str(out_path),
        "plans_written": int(written),
        "plan_kinds": dict(counts),
        "run_id": _infer_run_id_from_filename(payload_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build orders_sendplan_<run_id>.jsonl from orders_payload_<run_id>.jsonl")
    ap.add_argument("--payload", required=True, help="Path to orders_payload_<run_id>.jsonl")
    ap.add_argument("--out", default="", help="Optional output path")
    ap.add_argument("--execute", default="0", help="0/1 (executor will attempt only if 1 and plan_kind actionable)")
    ap.add_argument("--force-one-plan", type=int, default=0, help="0/1. Keep exactly one PLAN_IBKR_PLACE_ORDER if present.")
    ap.add_argument("--max_lines", type=int, default=250_000)

    a = ap.parse_args()

    payload_path = Path(a.payload)
    if not payload_path.is_absolute():
        payload_path = Path.cwd() / payload_path

    out_path = Path(a.out) if a.out else None
    if out_path is None:
        rid = _infer_run_id_from_filename(payload_path)
        out_path = payload_path.parent / f"orders_sendplan_{rid}.jsonl"
    else:
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path

    res = build_sendplan(
        payload_path=payload_path,
        out_path=out_path,
        execute=_as_bool01(a.execute),
        force_one_plan=bool(int(a.force_one_plan)),
        max_lines=int(a.max_lines),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
