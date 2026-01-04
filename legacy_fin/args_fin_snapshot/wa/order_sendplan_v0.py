# args/wa/order_sendplan_v0.py
from __future__ import annotations

import argparse
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Tuple

SCHEMA_VERSION = "order_sendplan_v0"

_RX_PAYLOAD = re.compile(r"^orders_payload_(?P<rid>.+)\.jsonl$", re.IGNORECASE)

# Payload kinds we understand at this stage.
_PAYLOAD_KIND_NONE = {"PAYLOAD_NONE", "", "UNKNOWN"}
_PAYLOAD_KIND_IBKR_ORDER = {"PAYLOAD_IBKR_ORDER", "IBKR_ORDER", "PAYLOAD_KIND_IBKR_ORDER"}

# Plan kinds we produce.
PLAN_KIND_NONE = "SENDPLAN_NONE"
PLAN_KIND_UNKNOWN = "SENDPLAN_UNKNOWN"
PLAN_KIND_IBKR_PLACE_ORDER = "PLAN_IBKR_PLACE_ORDER"


def _u16() -> str:
    return uuid.uuid4().hex[:16]


def _as_bool01(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        try:
            return bool(int(v))
        except Exception:
            return False
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _payload_kind(obj: Dict[str, Any]) -> str:
    v = obj.get("payload_kind")
    return str(v or "").strip().upper() or "UNKNOWN"


def _infer_run_id_from_filename(payload_path: Path) -> str:
    m = _RX_PAYLOAD.match(payload_path.name)
    return m.group("rid") if m else "unknown"


@dataclass
class JsonlReadStats:
    """
    Streaming JSONL reader stats (mutable).
    """
    lines_seen: int = 0
    dicts_seen: int = 0
    parse_errors: int = 0
    truncated: bool = False


def _iter_jsonl_dicts(path: Path, stats: JsonlReadStats, *, max_lines: int = 250_000) -> Iterator[Dict[str, Any]]:
    """
    Stream JSONL dicts while updating `stats`.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if stats.lines_seen >= max_lines:
                stats.truncated = True
                break
            s = line.strip()
            if not s:
                continue
            stats.lines_seen += 1
            try:
                obj = json.loads(s)
            except Exception:
                stats.parse_errors += 1
                continue
            if isinstance(obj, dict):
                stats.dicts_seen += 1
                yield obj


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


def _copy_if_dict(dst: Dict[str, Any], src: Dict[str, Any], *, key: str) -> None:
    v = src.get(key)
    if isinstance(v, dict) and v:
        dst[key] = v


def _mk_plan(
    payload: Dict[str, Any],
    *,
    execute: bool,
    force_one_plan: bool,
    forced_done: bool,
    run_id_fallback: str,
) -> Tuple[Dict[str, Any], bool]:
    pk = _payload_kind(payload)
    run_id = payload.get("run_id") or run_id_fallback

    plan: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "index": payload.get("index"),
        "ts": payload.get("ts"),
        "instrument": payload.get("instrument"),
        "timeframe": payload.get("timeframe"),
        "env": payload.get("env"),

        # Executor gating bits:
        "execute": bool(execute),  # sendplan-level arming (CLI-level)
        "payload_execute": bool(payload.get("execute", False)),  # payload-level marker

        # Context for execution guard and audit:
        "ma_decision": payload.get("ma_decision") or payload.get("decision"),
        "intent_kind": payload.get("intent_kind") or payload.get("kind"),
        "mode": payload.get("mode"),
        "mode_source": payload.get("mode_source"),
        "gate_reason": payload.get("gate_reason"),
        "rule_ids": payload.get("rule_ids") if isinstance(payload.get("rule_ids"), list) else [],

        # Identity:
        "payload_id": payload.get("payload_id"),
        "payload_kind": pk,
        "plan_id": _u16(),
        "plan_kind": PLAN_KIND_NONE,
        "reason": payload.get("reason") or "payload_none",
    }

    # Legacy compatibility: executor reads ma_decision OR decision
    if plan.get("ma_decision") is not None and plan.get("decision") is None:
        plan["decision"] = plan.get("ma_decision")

    # Default: NONE
    if pk in _PAYLOAD_KIND_NONE:
        plan["plan_kind"] = PLAN_KIND_NONE
        plan["reason"] = payload.get("reason") or "payload_none"
        return plan, forced_done

    # Expose contract/order to top-level for executor (supports both shapes)
    _copy_if_dict(plan, payload, key="contract")
    _copy_if_dict(plan, payload, key="order")

    ibkr = payload.get("ibkr")
    if isinstance(ibkr, dict):
        c = ibkr.get("contract")
        o = ibkr.get("order")
        if isinstance(c, dict) and c:
            plan["contract"] = c
        if isinstance(o, dict) and o:
            plan["order"] = o

    # IBKR order payload => actionable plan
    if pk in _PAYLOAD_KIND_IBKR_ORDER:
        plan["plan_kind"] = PLAN_KIND_IBKR_PLACE_ORDER
        plan["reason"] = payload.get("reason") or "payload_ibkr_order"

        if force_one_plan:
            if forced_done:
                plan["plan_kind"] = PLAN_KIND_NONE
                plan["reason"] = "force_one_plan_only_first"
            else:
                forced_done = True

        return plan, forced_done

    # Anything else
    plan["plan_kind"] = PLAN_KIND_UNKNOWN
    plan["reason"] = f"unhandled_payload_kind:{pk}"
    return plan, forced_done


def build_sendplan(
    *,
    payload_path: Path,
    out_path: Path,
    execute: bool,
    force_one_plan: bool,
    probe_allow_first_actionable: bool,
    max_lines: int = 250_000,
) -> Dict[str, Any]:
    """
    Build orders_sendplan_<run_id>.jsonl from orders_payload_<run_id>.jsonl

    Standard:
    - atomic output
    - fail-loud
    - probe override (engineering smoke) is allowed ONLY when force_one_plan=True
    """
    run_id_inferred = _infer_run_id_from_filename(payload_path)

    if not payload_path.exists():
        _write_jsonl_atomic(out_path, iter(()))
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "PAYLOAD_NOT_FOUND",
            "payload_path": str(payload_path),
            "out_path": str(out_path),
            "run_id": run_id_inferred,
            "plans_written": 0,
        }

    if probe_allow_first_actionable and (not force_one_plan):
        # Hard safety: probe override must be paired with force_one_plan to avoid accidental mass-ALLOW
        _write_jsonl_atomic(out_path, iter(()))
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "PROBE_REQUIRES_FORCE_ONE_PLAN",
            "payload_path": str(payload_path),
            "out_path": str(out_path),
            "run_id": run_id_inferred,
            "plans_written": 0,
        }

    stats = JsonlReadStats()
    counts: Counter[str] = Counter()
    forced_done = False
    payload_dicts_consumed = 0
    actionable = 0
    probe_used = False

    def rows() -> Iterator[Dict[str, Any]]:
        nonlocal forced_done, payload_dicts_consumed, actionable, probe_used
        for payload in _iter_jsonl_dicts(payload_path, stats, max_lines=max_lines):
            payload_dicts_consumed += 1

            plan, forced_done = _mk_plan(
                payload,
                execute=execute,
                force_one_plan=force_one_plan,
                forced_done=forced_done,
                run_id_fallback=run_id_inferred,
            )

            # Engineering smoke: force ALLOW on the first actionable plan (single plan only).
            if probe_allow_first_actionable and (not probe_used):
                if str(plan.get("plan_kind") or "") == PLAN_KIND_IBKR_PLACE_ORDER:
                    plan["ma_decision"] = "ALLOW"
                    plan["decision"] = "ALLOW"
                    plan["gate_reason"] = ""
                    plan["reason"] = "probe_allow_override"
                    probe_used = True

            pk = str(plan.get("plan_kind") or "UNKNOWN")
            counts[pk] += 1
            if pk == PLAN_KIND_IBKR_PLACE_ORDER:
                actionable += 1

            yield plan

    written = _write_jsonl_atomic(out_path, rows())

    if stats.dicts_seen == 0:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_DICT_LINES_IN_PAYLOAD",
            "payload_path": str(payload_path),
            "out_path": str(out_path),
            "run_id": run_id_inferred,
            "payload_lines_seen": int(stats.lines_seen),
            "payload_dicts_seen": int(stats.dicts_seen),
            "payload_parse_errors": int(stats.parse_errors),
            "payload_truncated": bool(stats.truncated),
            "payload_dicts_consumed": int(payload_dicts_consumed),
            "plans_written": int(written),
        }

    if written == 0:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_PLANS_WRITTEN",
            "payload_path": str(payload_path),
            "out_path": str(out_path),
            "run_id": run_id_inferred,
            "payload_lines_seen": int(stats.lines_seen),
            "payload_dicts_seen": int(stats.dicts_seen),
            "payload_parse_errors": int(stats.parse_errors),
            "payload_truncated": bool(stats.truncated),
            "payload_dicts_consumed": int(payload_dicts_consumed),
            "plans_written": int(written),
            "plan_kinds": dict(counts),
        }

    all_none = (counts.get(PLAN_KIND_NONE, 0) == written)
    if all_none:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "SENDPLAN_ALL_NONE",
            "payload_path": str(payload_path),
            "out_path": str(out_path),
            "run_id": run_id_inferred,
            "execute": bool(execute),
            "force_one_plan": bool(force_one_plan),
            "probe_allow_first_actionable": bool(probe_allow_first_actionable),
            "probe_used": bool(probe_used),
            "payload_lines_seen": int(stats.lines_seen),
            "payload_dicts_seen": int(stats.dicts_seen),
            "payload_parse_errors": int(stats.parse_errors),
            "payload_truncated": bool(stats.truncated),
            "payload_dicts_consumed": int(payload_dicts_consumed),
            "plans_written": int(written),
            "plan_kinds": dict(counts),
        }

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "execute": bool(execute),
        "force_one_plan": bool(force_one_plan),
        "probe_allow_first_actionable": bool(probe_allow_first_actionable),
        "probe_used": bool(probe_used),
        "payload_path": str(payload_path),
        "out_path": str(out_path),
        "run_id": run_id_inferred,
        "payload_lines_seen": int(stats.lines_seen),
        "payload_dicts_seen": int(stats.dicts_seen),
        "payload_parse_errors": int(stats.parse_errors),
        "payload_truncated": bool(stats.truncated),
        "payload_dicts_consumed": int(payload_dicts_consumed),
        "plans_written": int(written),
        "plans_actionable": int(actionable),
        "plan_kinds": dict(counts),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build orders_sendplan_<run_id>.jsonl from orders_payload_<run_id>.jsonl")
    ap.add_argument("--payload", required=True, help="Path to orders_payload_<run_id>.jsonl")
    ap.add_argument("--out", default="", help="Optional output path")
    ap.add_argument("--execute", default="0", help="0/1 (executor will attempt only if 1 and plan_kind actionable)")
    ap.add_argument(
        "--force-one-plan",
        "--force_one_plan",
        dest="force_one_plan",
        type=int,
        default=0,
        help="0/1. Keep exactly one PLAN_IBKR_PLACE_ORDER if present.",
    )
    ap.add_argument(
        "--probe-allow-first-actionable",
        dest="probe_allow_first_actionable",
        type=int,
        default=0,
        help="0/1. Engineering smoke: force ALLOW for the first PLAN_IBKR_PLACE_ORDER. Requires --force-one-plan 1.",
    )
    ap.add_argument("--max-lines", "--max_lines", dest="max_lines", type=int, default=250_000)

    a = ap.parse_args()

    payload_path = Path(a.payload)
    if not payload_path.is_absolute():
        payload_path = Path.cwd() / payload_path

    if a.out:
        out_path = Path(a.out)
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path
    else:
        rid = _infer_run_id_from_filename(payload_path)
        out_path = payload_path.parent / f"orders_sendplan_{rid}.jsonl"

    res = build_sendplan(
        payload_path=payload_path,
        out_path=out_path,
        execute=_as_bool01(a.execute),
        force_one_plan=bool(int(a.force_one_plan)),
        probe_allow_first_actionable=bool(int(a.probe_allow_first_actionable)),
        max_lines=int(a.max_lines),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
