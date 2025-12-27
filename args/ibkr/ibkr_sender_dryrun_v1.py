from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


def iter_jsonl_strict(path: Path) -> Tuple[Iterable[Dict[str, Any]], int, int]:
    """
    Strict JSONL iterator:
    - returns (iterable, total_lines_seen, parse_errors)
    - counts parse errors (does not silently ignore)
    """
    total = 0
    errors = 0

    def gen():
        nonlocal total, errors
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                total += 1
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        yield obj
                    else:
                        errors += 1
                except Exception:
                    errors += 1
                    continue

    return gen(), total, errors


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _touch_empty_jsonl(path: Path) -> None:
    """
    Ensure the JSONL file exists even if we write zero records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _validate_contract(c: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(c, dict):
        return ["contract not a dict"]

    if c.get("conId") is None and not c.get("localSymbol"):
        errs.append("contract missing conId/localSymbol")
    if not c.get("secType"):
        errs.append("contract missing secType")
    if not c.get("exchange"):
        errs.append("contract missing exchange")
    if not c.get("currency"):
        errs.append("contract missing currency")
    return errs


def _validate_order(o: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(o, dict):
        return ["order not a dict"]

    act = str(o.get("action") or "").upper()
    if act not in {"BUY", "SELL"}:
        errs.append("order.action not BUY/SELL")

    ot = str(o.get("orderType") or "").upper()
    if not ot:
        errs.append("order.orderType missing")

    try:
        q = int(o.get("totalQuantity"))
        if q <= 0:
            errs.append("order.totalQuantity <= 0")
    except Exception:
        errs.append("order.totalQuantity not int")

    # STRICT: transmit must exist and must be False in dry-run
    if "transmit" not in o:
        errs.append("order.transmit missing (must be present and False in dry-run)")
    elif o.get("transmit") is not False:
        errs.append("order.transmit must be False in dry-run")

    if ot == "LMT" and o.get("lmtPrice") is None:
        errs.append("order.lmtPrice missing for LMT")

    return errs


def _order_ref(run_id: str, index: Any) -> str:
    ix = _norm(index) if index is not None else "NA"
    ref = f"ARGS_{run_id}_{ix}"
    return ref[:48]


def _idempotency_key(run_id: str, index: Any, contract: Dict[str, Any], order: Dict[str, Any]) -> str:
    parts = [
        run_id,
        _norm(index),
        _norm(contract.get("conId") or ""),
        _norm(contract.get("localSymbol") or ""),
        _norm(order.get("action")),
        _norm(order.get("orderType")),
        _norm(order.get("totalQuantity")),
        _norm(order.get("lmtPrice")),
        _norm(order.get("tif")),
    ]
    return _sha16("|".join(parts))


def dryrun_sender(
    *,
    payload_path: Path,
    out_sendplan_path: Path,
    max_errors: int = 50,
) -> Dict[str, Any]:
    """
    Dry-run sender: validates payloads and writes sendplan JSONL.
    DOES NOT connect to IBKR and DOES NOT send anything.

    STRICT invariants:
    - parse errors counted (not silently ignored)
    - run_id must be non-empty for ORDER/CANCEL_ALL
    - order.transmit must exist and must be False
    - duplicate idempotency_key is an ERROR
    - sendplan file always exists (even empty)
    """
    if out_sendplan_path.exists():
        out_sendplan_path.unlink()

    # IMPORTANT: create empty file upfront so artifact always exists
    _touch_empty_jsonl(out_sendplan_path)

    total = 0
    payload_none = 0
    payload_order = 0
    payload_cancel = 0
    skipped = 0

    plan_orders = 0
    plan_cancels = 0

    errors: List[str] = []
    seen_keys: Set[str] = set()
    dup_keys = 0

    gen, _, parse_errors = iter_jsonl_strict(payload_path)

    for p in gen:
        total += 1
        pk = str(p.get("payload_kind") or "").strip().upper()

        if pk == "PAYLOAD_NONE":
            payload_none += 1
            skipped += 1
            continue

        run_id = str(p.get("run_id") or "").strip()
        if not run_id:
            errors.append(f"{pk}: missing run_id")
            if len(errors) >= max_errors:
                break
            continue

        contract = p.get("contract")
        c_errs = _validate_contract(contract)
        if c_errs:
            errors.append(f"{pk}: " + "; ".join(c_errs))
            if len(errors) >= max_errors:
                break
            continue
        assert isinstance(contract, dict)

        if pk == "PAYLOAD_CANCEL_ALL":
            payload_cancel += 1
            plan = {
                "kind": "SENDPLAN_CANCEL_ALL",
                "dry_run": True,
                "run_id": run_id,
                "index": p.get("index"),
                "ts": p.get("ts"),
                "orderRef": _order_ref(run_id, p.get("index")),
                "contract": contract,
                "reason": p.get("reason") or "CANCEL_ALL",
            }
            append_jsonl(out_sendplan_path, plan)
            plan_cancels += 1
            continue

        if pk != "PAYLOAD_ORDER":
            skipped += 1
            continue

        payload_order += 1
        order = p.get("order")
        o_errs = _validate_order(order)
        if o_errs:
            errors.append(f"{pk}: " + "; ".join(o_errs))
            if len(errors) >= max_errors:
                break
            continue
        assert isinstance(order, dict)

        idx = p.get("index")
        ref = _order_ref(run_id, idx)

        key = _idempotency_key(run_id, idx, contract, order)
        if key in seen_keys:
            dup_keys += 1
            errors.append(f"{pk}: duplicate idempotency_key={key}")
            if len(errors) >= max_errors:
                break
            continue
        seen_keys.add(key)

        plan = {
            "kind": "SENDPLAN_ORDER",
            "dry_run": True,
            "run_id": run_id,
            "index": idx,
            "ts": p.get("ts"),
            "idempotency_key": key,
            "orderRef": ref,
            "contract": contract,
            "order": order,
            "reason": p.get("reason") or "OK",
            "notes": p.get("notes") if isinstance(p.get("notes"), dict) else {},
        }
        append_jsonl(out_sendplan_path, plan)
        plan_orders += 1

    return {
        "payload_path": str(payload_path),
        "sendplan_out": str(out_sendplan_path),
        "payload_total": total,
        "payload_none": payload_none,
        "payload_order": payload_order,
        "payload_cancel_all": payload_cancel,
        "plan_orders": plan_orders,
        "plan_cancel_all": plan_cancels,
        "skipped": skipped,
        "dup_idempotency_keys": dup_keys,
        "parse_errors": parse_errors,
        "errors_count": len(errors),
        "errors_head": errors[:8],
    }
