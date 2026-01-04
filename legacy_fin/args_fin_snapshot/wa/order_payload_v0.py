# args/wa/order_payload_v0.py
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

SCHEMA_VERSION = "order_payload_v0"

PAYLOAD_KIND_NONE = "PAYLOAD_NONE"
PAYLOAD_KIND_IBKR_ORDER = "IBKR_ORDER"

_RX_INTENTS = re.compile(r"^order_intents_(?P<rid>.+)\.jsonl$", re.IGNORECASE)


@dataclass(frozen=True)
class PayloadBuildConfig:
    execute: bool = False            # metadata only; executor is controlled elsewhere
    default_qty: int = 1
    order_type: str = "MKT"
    tif: str = "DAY"

    # engineering probe
    force_one_order: bool = False
    force_order_side: str = "BUY"
    force_order_symbol: str = "AAPL"   # SAFE default (NOT HG)


@dataclass
class JsonlReadStats:
    lines_seen: int = 0
    dicts_seen: int = 0
    parse_errors: int = 0
    truncated: bool = False


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


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _normalize_side(x: Any, *, default: str = "BUY") -> str:
    s = _u(x)
    if s in ("BUY", "SELL"):
        return s
    return default


def _infer_run_id_from_intents_path(intents_path: Path) -> str:
    m = _RX_INTENTS.match(intents_path.name)
    return m.group("rid") if m else "unknown"


def _iter_jsonl_dicts(path: Path, stats: JsonlReadStats, *, max_lines: int = 250_000) -> Iterator[Dict[str, Any]]:
    """
    Stream JSONL dicts while updating stats. Single pass, safe for large files.
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


def _write_jsonl_atomic(out_path: Path, rows: Iterator[Dict[str, Any]]) -> int:
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


def _default_out_path(intents_path: Path) -> Path:
    rid = _infer_run_id_from_intents_path(intents_path)
    return intents_path.parent / f"orders_payload_{rid}.jsonl"


def _repo_root() -> Path:
    # .../args/wa/order_payload_v0.py -> repo root
    return Path(__file__).resolve().parents[2]


def _load_resolved_fut_contract(sym: str) -> Optional[Dict[str, Any]]:
    """
    Load resolved IBKR FUT contract dict produced by:
      args/ibkr/ibkr_contract_resolver_v1.py

    Files:
      args/data/ibkr_mhg_contract_v1.json
      args/data/ibkr_qc_contract_v1.json

    Returns dict with conId if present, else None.
    """
    s = str(sym or "").strip().upper()
    if not s:
        return None
    p = _repo_root() / "args" / "data" / f"ibkr_{s.lower()}_contract_v1.json"
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        if isinstance(obj, dict) and obj.get("conId"):
            return obj
        return None
    except Exception:
        return None


def _normalize_intent_kind(intent: Dict[str, Any]) -> str:
    """
    Normalize intent kinds:
      INTENT_NONE -> NONE
      INTENT_ENTRY -> ENTRY
      etc.
    """
    raw = intent.get("intent_kind") or intent.get("kind") or intent.get("type") or ""
    k = _u(raw)
    if k.startswith("INTENT_"):
        k = k[len("INTENT_") :]
    return k


def intent_to_payload(intent: Dict[str, Any], cfg: PayloadBuildConfig) -> Dict[str, Any]:
    """
    Converts one intent dict → payload dict.
    Always returns a dict payload (PAYLOAD_NONE or IBKR_ORDER).
    """
    run_id = intent.get("run_id")
    idx = intent.get("index")
    ts = intent.get("ts")
    instrument = intent.get("instrument")
    timeframe = intent.get("timeframe")
    env = intent.get("env")

    k_norm = _normalize_intent_kind(intent)

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "index": idx,
        "ts": ts,
        "instrument": instrument,
        "timeframe": timeframe,
        "env": env,

        # audit context propagated downstream
        "ma_decision": intent.get("ma_decision") or intent.get("decision"),
        "gate_reason": intent.get("gate_reason"),
        "mode": intent.get("mode"),
        "mode_source": intent.get("mode_source"),
        "rule_ids": intent.get("rule_ids") if isinstance(intent.get("rule_ids"), list) else [],

        # payload metadata
        "execute": bool(cfg.execute),
        "intent_kind": k_norm,
        "intent_kind_raw": intent.get("intent_kind") or intent.get("kind") or intent.get("type") or "",
        "payload_id": intent.get("intent_id") or intent.get("id") or "",
        "payload_kind": PAYLOAD_KIND_NONE,
        "reason": "intent_none",
    }

    # Legacy compat
    if payload.get("ma_decision") is not None and payload.get("decision") is None:
        payload["decision"] = payload.get("ma_decision")

    # If intent explicitly says NONE/NO_ACTION/NO_TRADE -> payload none
    if (not k_norm) or (k_norm in ("NONE", "NO_ACTION", "NO_TRADE")):
        payload["payload_kind"] = PAYLOAD_KIND_NONE
        payload["reason"] = "intent_none"
        return payload

    # If intent already contains ibkr payload (passthrough)
    ibkr = intent.get("ibkr")
    if isinstance(ibkr, dict) and isinstance(ibkr.get("contract"), dict) and isinstance(ibkr.get("order"), dict):
        payload["payload_kind"] = PAYLOAD_KIND_IBKR_ORDER
        payload["ibkr"] = ibkr
        payload["reason"] = "intent_ibkr_passthrough"
        return payload

    # Default: non-none intent without explicit ibkr mapping -> keep as PAYLOAD_NONE
    payload["payload_kind"] = PAYLOAD_KIND_NONE
    payload["reason"] = "intent_no_ibkr_mapping"
    return payload


def write_orders_payload(
    *,
    intents_path: Path,
    out_path: Optional[Path] = None,
    cfg: Optional[PayloadBuildConfig] = None,
    max_lines: int = 250_000,
) -> Dict[str, Any]:
    cfg = cfg or PayloadBuildConfig()
    out_path = out_path or _default_out_path(intents_path)

    if not intents_path.exists():
        _write_jsonl_atomic(out_path, iter(()))
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "INTENTS_NOT_FOUND",
            "intents_path": str(intents_path),
            "out_path": str(out_path),
            "payload_written": 0,
        }

    stats = JsonlReadStats()
    total = 0
    none_cnt = 0
    ibkr_cnt = 0
    forced_done = False

    def rows() -> Iterator[Dict[str, Any]]:
        nonlocal total, none_cnt, ibkr_cnt, forced_done
        for intent in _iter_jsonl_dicts(intents_path, stats, max_lines=max_lines):
            total += 1
            payload = intent_to_payload(intent, cfg)

            # Engineering probe: force exactly one IBKR order payload.
            # Standard:
            # - If resolved FUT contract exists (MHG/QC), use FUT (conId/localSymbol).
            # - Else fallback to SAFE STK probe.
            if cfg.force_one_order and (not forced_done):
                payload["payload_kind"] = PAYLOAD_KIND_IBKR_ORDER
                payload["reason"] = "force_one_order_probe"

                sym = str(cfg.force_order_symbol or "AAPL").strip().upper()
                side = _normalize_side(cfg.force_order_side, default="BUY")
                qty = int(cfg.default_qty) if int(cfg.default_qty) > 0 else 1

                payload["instrument"] = sym

                fut = _load_resolved_fut_contract(sym)
                if fut is not None:
                    payload["ibkr"] = {
                        "contract": fut,
                        "order": {
                            "action": side,
                            "orderType": str(cfg.order_type),
                            "totalQuantity": qty,
                            "tif": str(cfg.tif),
                        },
                    }
                    payload["reason"] = "force_one_order_probe_fut_resolved"
                else:
                    payload["ibkr"] = {
                        "contract": {"secType": "STK", "symbol": sym, "exchange": "SMART", "currency": "USD"},
                        "order": {
                            "action": side,
                            "orderType": str(cfg.order_type),
                            "totalQuantity": qty,
                            "tif": str(cfg.tif),
                        },
                    }

                forced_done = True

            if payload.get("payload_kind") == PAYLOAD_KIND_IBKR_ORDER:
                ibkr_cnt += 1
            else:
                none_cnt += 1

            yield payload

    written = _write_jsonl_atomic(out_path, rows())

    # HARD FAIL: truly no dict lines in intents
    if stats.dicts_seen == 0:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_DICT_LINES_IN_INTENTS",
            "intents_path": str(intents_path),
            "out_path": str(out_path),
            "intents_lines_seen": int(stats.lines_seen),
            "intents_dicts_seen": int(stats.dicts_seen),
            "intents_parse_errors": int(stats.parse_errors),
            "intents_truncated": bool(stats.truncated),
            "payload_written": int(written),
        }

    # HARD FAIL: wrote nothing (should never happen if dicts_seen>0)
    if written == 0:
        return {
            "ok": False,
            "schema_version": SCHEMA_VERSION,
            "error": "NO_PAYLOAD_WRITTEN",
            "intents_path": str(intents_path),
            "out_path": str(out_path),
            "intents_lines_seen": int(stats.lines_seen),
            "intents_dicts_seen": int(stats.dicts_seen),
            "intents_parse_errors": int(stats.parse_errors),
            "intents_truncated": bool(stats.truncated),
            "intents_consumed": int(total),
            "payload_written": int(written),
            "payload_none": int(none_cnt),
            "payload_ibkr_order": int(ibkr_cnt),
        }

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "intents_path": str(intents_path),
        "out_path": str(out_path),
        "execute": bool(cfg.execute),
        "force_one_order": bool(cfg.force_one_order),
        "intents_lines_seen": int(stats.lines_seen),
        "intents_dicts_seen": int(stats.dicts_seen),
        "intents_parse_errors": int(stats.parse_errors),
        "intents_truncated": bool(stats.truncated),
        "intents_consumed": int(total),
        "payload_written": int(written),
        "payload_none": int(none_cnt),
        "payload_ibkr_order": int(ibkr_cnt),
        "run_id": _infer_run_id_from_intents_path(intents_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="order_payload_v0",
        description="Build orders_payload_<run_id>.jsonl from order_intents_<run_id>.jsonl",
    )
    ap.add_argument("--intents", required=True, help="Path to order_intents_<run_id>.jsonl")
    ap.add_argument("--out", default="", help="Optional output path (default derived from intents filename)")
    ap.add_argument("--execute", default="0", help="0/1. Metadata only. Executor is controlled elsewhere.")
    ap.add_argument("--default-qty", type=int, default=1)
    ap.add_argument("--order-type", default="MKT", help="MKT/LMT (probe only)")
    ap.add_argument("--tif", default="DAY", help="DAY/GTC (probe only)")

    ap.add_argument("--force-one-order", type=int, default=0, help="0/1. Force exactly one IBKR order payload (probe).")
    ap.add_argument("--force-order-side", default="BUY", help="BUY/SELL (only used with --force-one-order 1)")
    ap.add_argument("--force-order-symbol", default="AAPL", help="SAFE default AAPL (only used with --force-one-order 1)")
    ap.add_argument("--max-lines", type=int, default=250_000)

    args = ap.parse_args()

    intents_path = Path(args.intents)
    if not intents_path.is_absolute():
        intents_path = Path.cwd() / intents_path

    out_path = Path(args.out) if args.out else None
    if out_path is not None and (not out_path.is_absolute()):
        out_path = Path.cwd() / out_path

    cfg = PayloadBuildConfig(
        execute=_as_bool01(args.execute),
        default_qty=int(args.default_qty),
        order_type=str(args.order_type),
        tif=str(args.tif),
        force_one_order=bool(int(args.force_one_order)),
        force_order_side=_normalize_side(args.force_order_side, default="BUY"),
        force_order_symbol=str(args.force_order_symbol or "AAPL"),
    )

    summary = write_orders_payload(intents_path=intents_path, out_path=out_path, cfg=cfg, max_lines=int(args.max_lines))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
