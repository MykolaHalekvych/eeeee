from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
            pk = str(obj.get("plan_kind") or "").strip().upper()
            if pk.startswith("PLAN_IBKR_"):
                return obj
    raise RuntimeError(f"No actionable PLAN_IBKR_* found in {sendplan_path}")


def _load_json_if_exists(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    return obj if isinstance(obj, dict) else None


def _find_contract_like(d: Any) -> Optional[Dict[str, Any]]:
    """
    Recursively search for a dict that has conId and/or localSymbol.
    Returns first match found.
    """
    if isinstance(d, dict):
        conid = d.get("conId")
        ls = d.get("localSymbol")
        if isinstance(conid, int) or (isinstance(ls, str) and ls.strip()):
            return d

        for v in d.values():
            m = _find_contract_like(v)
            if m is not None:
                return m

    if isinstance(d, list):
        for v in d:
            m = _find_contract_like(v)
            if m is not None:
                return m

    return None


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

    # Try contract from plan first
    contract = None
    for k in ("contract", "ibkr_contract"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            contract = v
            break

    # Fallback: resolver artifact
    if contract is None:
        resolver_path = repo_root / "args" / "data" / "ibkr_hg_contract_v1.json"
        resolver_obj = _load_json_if_exists(resolver_path)
        if resolver_obj is not None:
            contract = _find_contract_like(resolver_obj)

    # Last resort: raw HG contract json if you have it elsewhere (optional)
    if contract is None:
        raise RuntimeError("Cannot locate contract dict with conId/localSymbol in plan or ibkr_hg_contract_v1.json")

    # Ensure match keys exist
    conid = contract.get("conId")
    local_symbol = contract.get("localSymbol")
    if conid is None and (not isinstance(local_symbol, str) or not local_symbol.strip()):
        raise RuntimeError("Contract has neither conId nor localSymbol; cannot build match snapshot")

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
                "out_path": str(out_path),
                "match_keys": {"conId": conid, "localSymbol": local_symbol},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
