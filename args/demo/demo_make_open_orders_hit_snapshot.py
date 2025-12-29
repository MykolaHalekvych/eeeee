from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _resolve_sendplan(repo_root: Path, run_id: str) -> Path:
    p = repo_root / "args" / "data" / f"orders_sendplan_{run_id}.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"sendplan not found: {p}")
    return p


def _read_first_actionable_plan(sendplan_path: Path) -> Dict[str, Any]:
    with sendplan_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            obj = json.loads(s)
            if not isinstance(obj, dict):
                continue
            pk = _u(obj.get("plan_kind"))
            if pk.startswith("PLAN_IBKR_"):
                return obj
    raise RuntimeError(f"No actionable PLAN_IBKR_* found in {sendplan_path}")


def _load_json_dict(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    return obj if isinstance(obj, dict) else None


def _iter_dicts(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _score_contract(d: Dict[str, Any]) -> int:
    """
    Prefer:
      conId (best) > localSymbol > symbol
      + presence of secType/exchange/currency for signature matching
    """
    score = 0

    conid = d.get("conId")
    if isinstance(conid, int):
        score += 100

    ls = d.get("localSymbol")
    if isinstance(ls, str) and ls.strip():
        score += 50

    sym = d.get("symbol")
    if isinstance(sym, str) and sym.strip():
        score += 30

    if isinstance(d.get("secType"), str) and str(d.get("secType")).strip():
        score += 10
    if isinstance(d.get("exchange"), str) and str(d.get("exchange")).strip():
        score += 10
    if isinstance(d.get("currency"), str) and str(d.get("currency")).strip():
        score += 5

    # small extras
    if isinstance(d.get("tradingClass"), str) and str(d.get("tradingClass")).strip():
        score += 3
    if d.get("multiplier") is not None:
        score += 1
    if isinstance(d.get("lastTradeDateOrContractMonth"), str) and str(d.get("lastTradeDateOrContractMonth")).strip():
        score += 1

    return score


def _pick_best_contract(candidate_objs: Iterable[Any]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_score = -1

    for obj in candidate_objs:
        for d in _iter_dicts(obj):
            # must look like a contract at least a bit
            if not any(k in d for k in ("conId", "localSymbol", "symbol")):
                continue
            sc = _score_contract(d)
            if sc > best_score:
                best = d
                best_score = sc

    return dict(best) if best is not None else None


def _normalize_contract_for_signature(contract: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure signature fields exist as strings if possible.
    Does NOT invent symbol. If symbol missing, leave as-is (then match must use conId/localSymbol).
    """
    out = dict(contract)

    sym = out.get("symbol")
    if isinstance(sym, str) and sym.strip():
        # normalize casing for stable signature
        out["symbol"] = sym.strip().upper()

    # normalize typical futures fields if present
    for k in ("secType", "exchange", "currency", "localSymbol", "tradingClass"):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = v.strip()

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--order-id", type=int, default=999001)
    args = ap.parse_args()

    repo_root = _repo_root()
    run_id = str(args.run_id).strip()

    sendplan_path = _resolve_sendplan(repo_root, run_id)
    plan = _read_first_actionable_plan(sendplan_path)

    # candidate sources
    candidates: list[Any] = []

    # 1) contract in sendplan (best, if exists)
    for k in ("contract", "ibkr_contract"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            candidates.append(v)

    # 2) resolver artifact
    resolver_path = repo_root / "args" / "data" / "ibkr_hg_contract_v1.json"
    resolver_obj = _load_json_dict(resolver_path)
    if resolver_obj is not None:
        candidates.append(resolver_obj)

    contract = _pick_best_contract(candidates)
    if contract is None:
        raise RuntimeError("Cannot locate any contract-like dict (conId/localSymbol/symbol) in sendplan or resolver")

    contract = _normalize_contract_for_signature(contract)

    out_path = Path(str(args.out))
    if not out_path.is_absolute():
        out_path = repo_root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tag = f"recon_hit_{run_id}_{int(time.time())}"
    ts = _now_utc_iso()

    start = {
        "schema_version": "ibkr_open_orders_snapshot_v0",
        "kind": "IBKR_SNAPSHOT_START",
        "event_id": f"IBKR_SNAPSHOT_START:{tag}",
        "ts": ts,
        "tag": tag,
        "conn": {"host": "127.0.0.1", "port": 7497, "client_id": 11, "timeout_s": 15.0},
        "all_open": True,
        "source": "IBKR_OPEN_ORDERS_SNAPSHOT_V0",
    }

    open_order = {
        "schema_version": "ibkr_open_orders_snapshot_v0",
        "kind": "IBKR_OPEN_ORDER",
        "event_id": f"IBKR_OPEN_ORDER:{tag}:{int(args.order_id)}",
        "ts": ts,
        "tag": tag,
        "order_id": int(args.order_id),
        "contract": contract,
        "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT", "tif": "DAY", "transmit": False},
        "order_state": {"status": "Submitted"},
        "source": "IBKR_OPEN_ORDERS_SNAPSHOT_V0",
    }

    end_open = {
        "schema_version": "ibkr_open_orders_snapshot_v0",
        "kind": "IBKR_OPEN_ORDER_END",
        "event_id": f"IBKR_OPEN_ORDER_END:{tag}",
        "ts": ts,
        "tag": tag,
        "source": "IBKR_OPEN_ORDERS_SNAPSHOT_V0",
    }

    end = {
        "schema_version": "ibkr_open_orders_snapshot_v0",
        "kind": "IBKR_SNAPSHOT_END",
        "event_id": f"IBKR_SNAPSHOT_END:{tag}",
        "ts": ts,
        "tag": tag,
        "counts": {"open_orders": 1, "status_events": 0, "infos": 0, "errors": 0},
        "source": "IBKR_OPEN_ORDERS_SNAPSHOT_V0",
    }

    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(start, ensure_ascii=False) + "\n")
        f.write(json.dumps(open_order, ensure_ascii=False) + "\n")
        f.write(json.dumps(end_open, ensure_ascii=False) + "\n")
        f.write(json.dumps(end, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "ok": True,
                "run_id": run_id,
                "sendplan_path": str(sendplan_path),
                "resolver_path": str(resolver_path),
                "out_path": str(out_path),
                "contract_keys": {
                    "conId": contract.get("conId"),
                    "localSymbol": contract.get("localSymbol"),
                    "symbol": contract.get("symbol"),
                    "secType": contract.get("secType"),
                    "exchange": contract.get("exchange"),
                    "currency": contract.get("currency"),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
