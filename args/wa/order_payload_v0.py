# args/wa/order_payload_v0.py
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple


# -----------------------------
# Schema / contract
# -----------------------------
SCHEMA_VERSION = "order_payload_v0"

# What we emit into orders_payload_*.jsonl
PAYLOAD_KIND_NONE = "PAYLOAD_NONE"
PAYLOAD_KIND_IBKR_ORDER = "IBKR_ORDER"


@dataclass(frozen=True)
class PayloadBuildConfig:
    execute: bool = False            # SAFE DEFAULT: never send real orders from this stage
    default_qty: int = 1             # used only if intent doesn't specify qty
    order_type: str = "MKT"          # MKT only for stub payloads
    tif: str = "DAY"                 # day orders for stub payloads


# -----------------------------
# JSONL helpers (BOM-safe)
# -----------------------------
def _iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
        yield  # pragma: no cover
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")
            n += 1
    return n


# -----------------------------
# Intent -> payload mapping
# -----------------------------
def _s(v: Any) -> str:
    return str(v) if v is not None else ""


def _u(v: Any) -> str:
    return _s(v).strip().upper()


def _payload_id(intent: Dict[str, Any]) -> str:
    # stable + short id for dedupe / tracing
    raw = "|".join([
        _s(intent.get("run_id")),
        _s(intent.get("index")),
        _u(intent.get("kind") or intent.get("kind_raw")),
        _u(intent.get("instrument")),
        _s(intent.get("ts")),
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _is_entry_intent(kind_u: str) -> bool:
    # future-proof: any "ENTER" is treated as new entry
    return "ENTER" in kind_u or "OPEN" in kind_u or "NEW" in kind_u


def _is_exit_intent(kind_u: str) -> bool:
    # future-proof: any "EXIT"/"CLOSE" is treated as exit
    return "EXIT" in kind_u or "CLOSE" in kind_u


def _side_from_kind(kind_u: str) -> Optional[str]:
    # very conservative mapping; extend later when strategy defines kinds.
    if "LONG" in kind_u or "BUY" in kind_u:
        return "BUY"
    if "SHORT" in kind_u or "SELL" in kind_u:
        return "SELL"
    return None


def intent_to_payload(intent: Dict[str, Any], cfg: PayloadBuildConfig) -> Dict[str, Any]:
    run_id = _s(intent.get("run_id"))
    idx = intent.get("index")
    ts = intent.get("ts")

    kind_u = _u(intent.get("kind") or intent.get("kind_raw"))
    instrument = _s(intent.get("instrument"))
    timeframe = _s(intent.get("timeframe") or intent.get("timeframe"))
    env = _s(intent.get("env"))

    mode = _u(intent.get("mode"))
    mode_source = _u(intent.get("mode_source"))
    gate_reason = _s(intent.get("gate_reason"))
    rule_ids = intent.get("rule_ids")
    if not isinstance(rule_ids, list):
        rule_ids = []

    allow_new_entries = bool(intent.get("allow_new_entries", False))
    allow_exits = bool(intent.get("allow_exits", False))

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "payload_id": _payload_id(intent),
        "payload_kind": PAYLOAD_KIND_NONE,  # default
        "execute": bool(cfg.execute),        # SAFE DEFAULT = False
        "run_id": run_id,
        "index": idx,
        "ts": ts,
        "instrument": instrument,
        "timeframe": timeframe,
        "env": env,
        "intent_kind": kind_u or "UNKNOWN",
        "mode": mode or None,
        "mode_source": mode_source or None,
        "gate_reason": gate_reason or None,
        "rule_ids": rule_ids,
    }

    # If intent is NONE -> payload stays NONE
    if kind_u in ("INTENT_NONE", "NONE", ""):
        payload["payload_kind"] = PAYLOAD_KIND_NONE
        payload["reason"] = "intent_none"
        return payload

    # Hard safety gates (belt + suspenders):
    if _is_entry_intent(kind_u) and (not allow_new_entries):
        payload["payload_kind"] = PAYLOAD_KIND_NONE
        payload["reason"] = "blocked_allow_new_entries=false"
        return payload

    if _is_exit_intent(kind_u) and (not allow_exits):
        payload["payload_kind"] = PAYLOAD_KIND_NONE
        payload["reason"] = "blocked_allow_exits=false"
        return payload

    # Otherwise: produce an IBKR_ORDER stub payload (still execute=False by default)
    side = _side_from_kind(kind_u) or "BUY"  # default BUY for any non-mapped kind (placeholder)
    qty = intent.get("qty")
    try:
        qty_i = int(qty) if qty is not None else int(cfg.default_qty)
        if qty_i <= 0:
            qty_i = int(cfg.default_qty)
    except Exception:
        qty_i = int(cfg.default_qty)

    payload["payload_kind"] = PAYLOAD_KIND_IBKR_ORDER
    payload["ibkr"] = {
        # NOTE: This is a "contract hint" stub. Real contract resolution happens later.
        "contract": {
            "symbol": instrument or "UNKNOWN",
            "secType": "FUT",
            "exchange": "SMART",
            "currency": "USD",
        },
        "order": {
            "action": side,
            "orderType": str(cfg.order_type),
            "totalQuantity": qty_i,
            "tif": str(cfg.tif),
        },
    }
    payload["reason"] = "ok"
    return payload


def _default_out_path(intents_path: Path) -> Path:
    # expects: order_intents_<run_id>.jsonl
    name = intents_path.name
    run_id = name
    if name.lower().startswith("order_intents_"):
        run_id = name[len("order_intents_") :]
    if run_id.lower().endswith(".jsonl"):
        run_id = run_id[: -len(".jsonl")]
    return intents_path.parent / f"orders_payload_{run_id}.jsonl"


def write_orders_payload(intents_path: Path, out_path: Optional[Path] = None, cfg: Optional[PayloadBuildConfig] = None) -> Dict[str, Any]:
    cfg = cfg or PayloadBuildConfig()
    out_path = out_path or _default_out_path(intents_path)

    total = 0
    none_cnt = 0
    ibkr_cnt = 0

    def rows() -> Iterator[Dict[str, Any]]:
        nonlocal total, none_cnt, ibkr_cnt
        for intent in _iter_jsonl(intents_path):
            total += 1
            payload = intent_to_payload(intent, cfg)
            if payload.get("payload_kind") == PAYLOAD_KIND_IBKR_ORDER:
                ibkr_cnt += 1
            else:
                none_cnt += 1
            yield payload

    written = _write_jsonl(out_path, rows())

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "intents_path": str(intents_path),
        "out_path": str(out_path),
        "execute": bool(cfg.execute),
        "intents_total": total,
        "payload_written": written,
        "payload_none": none_cnt,
        "payload_ibkr_order": ibkr_cnt,
    }


# -----------------------------
# CLI
# -----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(prog="order_payload_v0", description="Build orders_payload_<run_id>.jsonl from order_intents_<run_id>.jsonl")
    ap.add_argument("--intents", required=True, help="Path to order_intents_<run_id>.jsonl")
    ap.add_argument("--out", default="", help="Optional output path (default derived from intents filename)")
    ap.add_argument("--execute", default="0", help="0/1. SAFE DEFAULT=0. Keep 0 for paper.")
    ap.add_argument("--default-qty", type=int, default=1)
    args = ap.parse_args()

    intents_path = Path(args.intents)
    out_path = Path(args.out) if args.out else None
    cfg = PayloadBuildConfig(
        execute=str(args.execute).strip() in ("1", "true", "TRUE", "yes", "YES"),
        default_qty=int(args.default_qty),
    )

    summary = write_orders_payload(intents_path=intents_path, out_path=out_path, cfg=cfg)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
