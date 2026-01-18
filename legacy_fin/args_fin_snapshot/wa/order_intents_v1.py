# args/wa/order_intents_v1.py
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator

from args.wa.mode_gate_v1 import apply_mode_gate_from_report


SCHEMA_VERSION = "order_intents_v1"

# Canonical semantic kinds at this stage (keep small, auditable).
KIND_NONE = "NONE"
KIND_ENTRY = "ENTRY"
KIND_EXIT = "EXIT"
KIND_REDUCE = "REDUCE"
KIND_TAKE_PROFIT = "TAKE_PROFIT"
KIND_CANCEL_ALL = "CANCEL_ALL"

_ALLOWED_KINDS = {
    KIND_NONE,
    KIND_ENTRY,
    KIND_EXIT,
    KIND_REDUCE,
    KIND_TAKE_PROFIT,
    KIND_CANCEL_ALL,
}

# Optional: infer run_id from raw intents filename if needed
_RX_RAW = re.compile(r"^(raw_)?intents_(?P<rid>.+)\.jsonl$", re.IGNORECASE)


def _s(x: Any, default: str = "") -> str:
    s = str(x or "").strip()
    return s if s else default


def _i(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        if isinstance(x, bool):
            return int(x)
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x).strip()
        return int(s) if s else default
    except Exception:
        return default


def _norm_kind(x: Any) -> str:
    k = str(x or "").strip().upper().replace("-", "_")
    return k if k in _ALLOWED_KINDS else KIND_NONE


def enforce_mode_gate(intent: Any, run_report: Any) -> Dict[str, Any]:
    """
    Fail-safe wrapper for mode gate. Never throws; always returns a dict with kind.
    """
    if not isinstance(intent, dict):
        return {
            "kind": KIND_NONE,
            "kind_raw": str(intent),
            "gate_reason": "intent_not_dict",
        }
    rr = run_report if isinstance(run_report, dict) else {}
    try:
        out = apply_mode_gate_from_report(intent, rr)
        if isinstance(out, dict):
            return out
        return {
            "kind": KIND_NONE,
            "kind_raw": str(intent.get("kind")),
            "gate_reason": "mode_gate_not_dict",
        }
    except Exception as e:
        return {
            "kind": KIND_NONE,
            "kind_raw": str(intent.get("kind")),
            "gate_reason": f"mode_gate_exception:{type(e).__name__}",
        }


def _write_jsonl_atomic(out_path: Path, rows: Iterator[Dict[str, Any]]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        for obj in rows:
            f.write(
                json.dumps(
                    obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            f.write("\n")
            n += 1
    tmp.replace(out_path)
    return n


def normalize_order_intent(
    intent: Dict[str, Any], run_report: Dict[str, Any], source: str = "wa_v1"
) -> Dict[str, Any]:
    """
    Contract record for order_intents_v1 (stable, auditable).
    Keeps fields needed downstream: run_id/index/ts/instrument/timeframe/env/kind/intent_kind + gate context.
    """
    run_id = _s(intent.get("run_id") or run_report.get("run_id"), "")
    if not run_id:
        raise ValueError("order_intents_v1: run_id missing (intent/run_report)")

    instrument = _s(intent.get("instrument"), _s(run_report.get("instrument"), "HG"))
    timeframe = _s(intent.get("timeframe"), _s(run_report.get("timeframe"), "5m"))
    env = _s(intent.get("env"), _s(run_report.get("env"), "IBKR_PAPER_LABEL"))

    kind_norm = _norm_kind(intent.get("kind"))
    kind_raw = (
        intent.get("kind_raw")
        if intent.get("kind_raw") is not None
        else intent.get("kind")
    )

    rec: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "run_id": run_id,
        "index": _i(intent.get("index")),
        "ts": intent.get("ts"),
        "instrument": instrument,
        "timeframe": timeframe,
        "env": env,
        # decision context / gating
        "ma_decision": intent.get("ma_decision"),
        "mode": intent.get("mode"),
        "position_size": intent.get("position_size"),
        "gate_reason": intent.get("gate_reason") or None,
        # intent semantics
        "kind_raw": kind_raw,
        "kind": kind_norm,
        # downstream compatibility: payload builder can use intent_kind OR kind
        "intent_kind": kind_norm,
    }

    # Optional passthrough for future mapping (harmless if unused)
    wa_action = intent.get("wa_action")
    if isinstance(wa_action, dict) and wa_action:
        rec["wa_action"] = wa_action

    return rec


def build_order_intents(
    *,
    run_report: Dict[str, Any],
    raw_intents_path: Path,
    out_path: Path,
    source: str = "wa_v1",
    max_lines: int = 250_000,
) -> Dict[str, Any]:
    """
    Reads raw intents JSONL -> applies mode gate -> writes normalized order_intents JSONL.
    STANDARD:
      - atomic output
      - streaming processing (no preload)
      - fail-loud if no dicts
      - summary includes parse stats + kind breakdown
    """
    if not raw_intents_path.exists():
        _write_jsonl_atomic(out_path, iter(()))
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "RAW_INTENTS_NOT_FOUND",
            "raw_intents": str(raw_intents_path),
            "order_intents": str(out_path),
        }

    # Raw stats (computed during streaming)
    raw_lines_seen = 0
    raw_dicts_seen = 0
    raw_parse_errors = 0

    def raw_dicts() -> Iterator[Dict[str, Any]]:
        nonlocal raw_lines_seen, raw_dicts_seen, raw_parse_errors
        with raw_intents_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if raw_lines_seen >= max_lines:
                    break
                s = line.strip()
                if not s:
                    continue
                raw_lines_seen += 1
                try:
                    obj = json.loads(s)
                except Exception:
                    raw_parse_errors += 1
                    continue
                if isinstance(obj, dict):
                    raw_dicts_seen += 1
                    yield obj

    counts = Counter()
    total = 0
    errors = 0

    none = 0
    gated = 0
    allowed = 0
    cancel_all = 0
    exits = 0
    entries = 0

    def rows() -> Iterator[Dict[str, Any]]:
        nonlocal total, errors, none, gated, allowed, cancel_all, exits, entries
        for raw in raw_dicts():
            total += 1
            try:
                gated_intent = enforce_mode_gate(raw, run_report)
                k = _norm_kind(gated_intent.get("kind"))

                if k == KIND_NONE:
                    none += 1
                    if gated_intent.get("gate_reason"):
                        gated += 1
                else:
                    allowed += 1
                    if k == KIND_CANCEL_ALL:
                        cancel_all += 1
                    elif k in {KIND_EXIT, KIND_REDUCE, KIND_TAKE_PROFIT}:
                        exits += 1
                    elif k == KIND_ENTRY:
                        entries += 1

                rec = normalize_order_intent(gated_intent, run_report, source=source)
                counts[k] += 1
                yield rec

            except Exception as e:
                errors += 1
                # forensic line (keeps output non-corrupt for inspection)
                yield {
                    "schema_version": SCHEMA_VERSION,
                    "source": source,
                    "run_id": _s(run_report.get("run_id"), "unknown"),
                    "kind": KIND_NONE,
                    "intent_kind": KIND_NONE,
                    "gate_reason": "INTENTS_EXCEPTION",
                    "details": {"error": repr(e)},
                }

    written = _write_jsonl_atomic(out_path, rows())

    # fail-loud if raw had zero dicts
    if raw_dicts_seen == 0:
        # out already exists (atomic write), but empty
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_DICT_LINES_IN_RAW_INTENTS",
            "raw_intents": str(raw_intents_path),
            "order_intents": str(out_path),
            "raw_lines_seen": int(raw_lines_seen),
            "raw_dicts_seen": int(raw_dicts_seen),
            "raw_parse_errors": int(raw_parse_errors),
            "total": int(total),
            "written": int(written),
            "errors": int(errors),
        }

    # fail-loud if somehow wrote nothing
    if written == 0:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_ROWS_WRITTEN",
            "raw_intents": str(raw_intents_path),
            "order_intents": str(out_path),
            "raw_lines_seen": int(raw_lines_seen),
            "raw_dicts_seen": int(raw_dicts_seen),
            "raw_parse_errors": int(raw_parse_errors),
            "total": int(total),
            "written": int(written),
            "errors": int(errors),
        }

    ok = errors == 0

    return {
        "ok": bool(ok),
        "schema_version": SCHEMA_VERSION,
        "raw_intents": str(raw_intents_path),
        "order_intents": str(out_path),
        "raw_lines_seen": int(raw_lines_seen),
        "raw_dicts_seen": int(raw_dicts_seen),
        "raw_parse_errors": int(raw_parse_errors),
        "total": int(total),
        "written": int(written),
        "errors": int(errors),
        "allowed": int(allowed),
        "none": int(none),
        "gated": int(gated),
        "breakdown": {
            "entries": int(entries),
            "exits": int(exits),
            "cancel_all": int(cancel_all),
        },
        "kind_counts": dict(counts),
    }


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build normalized order_intents_<run_id>.jsonl from raw intents JSONL + run_report.json"
    )
    ap.add_argument(
        "--run-report",
        required=True,
        help="Path to run_report_<run_id>_paper.json or run_report_<run_id>.json",
    )
    ap.add_argument("--raw-intents", required=True, help="Path to raw intents JSONL")
    ap.add_argument(
        "--out", required=True, help="Output path order_intents_<run_id>.jsonl"
    )
    ap.add_argument("--source", default="wa_v1")
    ap.add_argument("--max-lines", type=int, default=250_000)

    a = ap.parse_args()

    rr = Path(a.run_report)
    if not rr.is_absolute():
        rr = Path.cwd() / rr

    ri = Path(a.raw_intents)
    if not ri.is_absolute():
        ri = Path.cwd() / ri

    out = Path(a.out)
    if not out.is_absolute():
        out = Path.cwd() / out

    run_report = _read_json(rr)
    res = build_order_intents(
        run_report=run_report,
        raw_intents_path=ri,
        out_path=out,
        source=str(a.source),
        max_lines=int(a.max_lines),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
