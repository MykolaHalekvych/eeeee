# args/stage5/terminal/terminal_proof_pack_v2.py
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -------------------------
# Utilities
# -------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha1_hex(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _lower_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in d.items():
        out[str(k).lower()] = v
    return out


def _dig_candidates(obj: Any) -> List[Dict[str, Any]]:
    """
    Return a list of dict candidates to search for fields.
    We try top-level, then common nested payload containers.
    """
    cands: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        cands.append(obj)
        lk = _lower_keys(obj)
        for key in ("data", "payload", "fields", "event", "msg", "message", "body"):
            v = lk.get(key)
            if isinstance(v, dict):
                cands.append(v)
    return cands


def get_any(obj: Any, keys: Tuple[str, ...]) -> Optional[Any]:
    if not isinstance(obj, dict):
        return None
    for cand in _dig_candidates(obj):
        lk = _lower_keys(cand)
        for k in keys:
            kk = k.lower()
            if kk in lk:
                return lk[kk]
    return None


def str_or_none(x: Any) -> Optional[str]:
    if x is None:
        return None
    try:
        s = str(x)
        return s if s != "" else None
    except Exception:
        return None


def int_or_none(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except Exception:
        return None


def float_or_none(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def iso_max(a: str, b: str) -> str:
    # ISO Z strings compare lexicographically for UTC; safe enough here.
    return a if a >= b else b


# -------------------------
# Ring buffer with fast "has"
# -------------------------

@dataclass
class Ring:
    max_items: int
    items: List[str] = field(default_factory=list)
    _set: set[str] = field(default_factory=set)

    @classmethod
    def from_list(cls, max_items: int, items: List[str]) -> "Ring":
        r = cls(max_items=max_items)
        for s in items[-max_items:]:
            r.add(s)
        return r

    def add(self, s: str) -> None:
        if not s:
            return
        if s in self._set:
            return
        self.items.append(s)
        self._set.add(s)
        if len(self.items) > self.max_items:
            drop = self.items[:-self.max_items]
            self.items = self.items[-self.max_items:]
            for d in drop:
                self._set.discard(d)

    def has(self, s: str) -> bool:
        if not s:
            return False
        return s in self._set

    def as_list(self) -> List[str]:
        return list(self.items)


# -------------------------
# Per-order aggregated state
# -------------------------

@dataclass
class OrderState:
    order_key: str
    order_uid: str
    symbol: str
    first_seen_utc: str
    last_seen_utc: str
    status_last: Optional[str] = None
    total_qty_last: Optional[float] = None
    filled_last: Optional[float] = None
    remaining_last: Optional[float] = None
    execs: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # exec_key -> {qty, price, ts, execId}
    terminal: Optional[Dict[str, Any]] = None  # {type, ts_utc, finalized, reason}
    anomalies: List[str] = field(default_factory=list)

    def cum_qty_from_execs(self) -> float:
        s = 0.0
        for e in self.execs.values():
            q = float_or_none(e.get("qty"))
            if q is not None:
                s += q
        return s


# -------------------------
# Event normalization
# -------------------------

def detect_kind(obj: Dict[str, Any]) -> str:
    if get_any(obj, ("execid", "execId", "executionid", "executionId")) is not None:
        return "EXECUTION"
    if get_any(obj, ("errorcode", "errorCode", "code")) is not None and get_any(obj, ("errormsg", "errorMsg", "message", "msg")) is not None:
        return "ERROR"
    if get_any(obj, ("status",)) is not None and get_any(obj, ("orderid", "orderId", "permid", "permId")) is not None:
        return "ORDER_STATUS"
    if get_any(obj, ("openorder", "openOrder", "order", "contract")) is not None and get_any(obj, ("orderid", "orderId", "permid", "permId")) is not None:
        return "OPEN_ORDER"
    return "UNKNOWN"


def normalize_event(obj: Dict[str, Any], ingest_ts_utc: str) -> Dict[str, Any]:
    kind = detect_kind(obj)

    ts_utc = str_or_none(get_any(obj, ("ts_utc", "ts", "timestamp", "time")))
    ts_inferred = False
    if ts_utc is None:
        ts_utc = ingest_ts_utc
        ts_inferred = True

    symbol = str_or_none(get_any(obj, ("symbol", "localsymbol", "localSymbol", "ticker"))) or "UNKNOWN"
    account = str_or_none(get_any(obj, ("account", "acct"))) or None

    order_id = int_or_none(get_any(obj, ("orderid", "orderId")))
    perm_id = int_or_none(get_any(obj, ("permid", "permId")))
    client_id = int_or_none(get_any(obj, ("clientid", "clientId")))
    order_ref = str_or_none(get_any(obj, ("orderref", "orderRef")))

    status = str_or_none(get_any(obj, ("status",)))
    total_qty = float_or_none(get_any(obj, ("totalqty", "totalQty", "quantity", "qty")))
    filled = float_or_none(get_any(obj, ("filled", "filledqty", "filledQty", "cumqty", "cumQty")))
    remaining = float_or_none(get_any(obj, ("remaining", "remainingqty", "remainingQty")))

    exec_id = str_or_none(get_any(obj, ("execid", "execId")))
    exec_qty = float_or_none(get_any(obj, ("execqty", "execQty", "lastfillqty", "lastFillQty", "shares", "qty")))
    exec_price = float_or_none(get_any(obj, ("execprice", "execPrice", "price")))

    error_code = int_or_none(get_any(obj, ("errorcode", "errorCode", "code")))
    error_msg = str_or_none(get_any(obj, ("errormsg", "errorMsg", "message", "msg")))

    # order_key priority: orderRef > permId > orderId
    if order_ref:
        order_key = f"oref:{order_ref}"
    elif perm_id is not None:
        order_key = f"perm:{perm_id}"
    elif order_id is not None:
        order_key = f"oid:{order_id}"
    else:
        order_key = "unknown:0"

    parts = [
        account or "",
        str(client_id) if client_id is not None else "",
        order_key,
        symbol,
    ]
    order_uid = "|".join(parts)

    # exec_key (prefer stable execId)
    if exec_id:
        exec_key = f"exec:{exec_id}"
    else:
        h = sha1_hex(
            json.dumps(
                {"symbol": symbol, "order_key": order_key, "qty": exec_qty, "price": exec_price, "ts": ts_utc},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        exec_key = f"exec_fallback:{h}"

    return {
        "ts_utc": ts_utc,
        "ts_inferred": ts_inferred,
        "kind": kind,
        "symbol": symbol,
        "account": account,
        "orderId": order_id,
        "permId": perm_id,
        "clientId": client_id,
        "orderRef": order_ref,
        "order_key": order_key,
        "order_uid": order_uid,
        "status": status,
        "total_qty": total_qty,
        "filled": filled,
        "remaining": remaining,
        "execId": exec_id,
        "exec_key": exec_key,
        "execQty": exec_qty,
        "execPrice": exec_price,
        "errorCode": error_code,
        "errorMsg": error_msg,
    }


# -------------------------
# Terminal signal detection
# -------------------------

def is_reject_event(ev: Dict[str, Any]) -> bool:
    st = ev.get("status") or ""
    st_l = st.lower() if isinstance(st, str) else ""
    if "reject" in st_l:
        return True

    if ev.get("kind") == "ERROR" and ev.get("errorCode") is not None:
        msg = ev.get("errorMsg") or ""
        msg_l = msg.lower() if isinstance(msg, str) else ""
        if "reject" in msg_l or "rejected" in msg_l or "denied" in msg_l or "not accepted" in msg_l:
            return True
    return False


def is_cancel_event(ev: Dict[str, Any]) -> bool:
    st = ev.get("status") or ""
    st_l = st.lower() if isinstance(st, str) else ""
    return "cancel" in st_l


def is_fill_event(ev: Dict[str, Any]) -> bool:
    st = ev.get("status") or ""
    st_l = st.lower() if isinstance(st, str) else ""
    if "filled" in st_l:
        return True
    total = ev.get("total_qty")
    filled = ev.get("filled")
    if isinstance(total, (int, float)) and isinstance(filled, (int, float)):
        if total > 0 and abs(float(filled) - float(total)) < 1e-9:
            return True
    return False


# -------------------------
# JSON helpers
# -------------------------

def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# -------------------------
# Main
# -------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--input-jsonl", default="")
    ap.add_argument("--cursor", default="")
    ap.add_argument("--out-root", default="")
    ap.add_argument("--max-orders", type=int, default=500)
    ap.add_argument("--ring-size", type=int, default=20000)
    ap.add_argument("--enrichment-window-sec", type=int, default=60)  # reserved for v2.2
    args = ap.parse_args()

    repo = Path(args.repo)
    in_jsonl = Path(args.input_jsonl) if args.input_jsonl else (repo / "args" / "data" / "ibkr_events_live.jsonl")
    cursor_path = Path(args.cursor) if args.cursor else (repo / "args" / "data" / "stage5_terminal_proof_v2.cursor.json")
    out_root = Path(args.out_root) if args.out_root else (repo / "args" / "stage5_terminal_proofs_v2")

    ingest_ts = utc_now_iso()
    warns: List[str] = []
    anomalies_top: List[str] = []

    latest_path = repo / "args" / "data" / "stage5_terminal_proof_v2_latest.json"

    if not in_jsonl.exists():
        out = {
            "schema": "stage5_terminal_proof_pack_v2_summary",
            "ts_utc": ingest_ts,
            "status": "FAIL",
            "exit_code": 2,
            "reason": "input_jsonl_missing",
            "input_jsonl": str(in_jsonl),
        }
        save_json(latest_path, out)
        return 2

    cur = load_json(cursor_path) or {}
    wm = cur.get("watermark") or {}
    last_off = int(wm.get("offset") or 0)

    try:
        st = in_jsonl.stat()
        size = int(st.st_size)
        mtime_utc = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception as e:
        out = {
            "schema": "stage5_terminal_proof_pack_v2_summary",
            "ts_utc": ingest_ts,
            "status": "FAIL",
            "exit_code": 2,
            "reason": f"stat_failed:{e}",
        }
        save_json(latest_path, out)
        return 2

    # If file truncated/rotated, reset watermark to 0
    if size < last_off:
        warns.append("watermark_reset_truncation_detected")
        last_off = 0

    recent_exec = Ring.from_list(max_items=args.ring_size, items=list(cur.get("recent_exec_keys") or []))
    # HARD RULE: only one terminal per order_key, so this tracks order_key only.
    terminal_emitted = Ring.from_list(max_items=args.ring_size, items=list(cur.get("terminal_emitted_keys") or []))

    # Rehydrate bounded orders
    orders_raw = cur.get("orders") or {}
    orders: Dict[str, OrderState] = {}
    for ok, ov in orders_raw.items():
        try:
            orders[ok] = OrderState(
                order_key=ok,
                order_uid=str(ov.get("order_uid") or ""),
                symbol=str(ov.get("symbol") or "UNKNOWN"),
                first_seen_utc=str(ov.get("first_seen_utc") or ingest_ts),
                last_seen_utc=str(ov.get("last_seen_utc") or ingest_ts),
                status_last=ov.get("status_last"),
                total_qty_last=ov.get("total_qty_last"),
                filled_last=ov.get("filled_last"),
                remaining_last=ov.get("remaining_last"),
                execs=dict(ov.get("execs") or {}),
                terminal=ov.get("terminal"),
                anomalies=list(ov.get("anomalies") or []),
            )
        except Exception:
            continue

    out_dir = out_root / (ingest_ts.replace(":", "").replace("-", "").replace(".", "") + "_terminal")
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_hash_seen: set[str] = set()
    dropped_raw = 0
    dropped_exec = 0
    dropped_terminal = 0

    processed_lines = 0
    processed_events = 0
    new_terminal_events: List[Dict[str, Any]] = []

    # read delta bytes
    with open(in_jsonl, "rb") as f:
        f.seek(last_off)
        chunk = f.read()
        new_off = f.tell()

    lines = chunk.splitlines()
    for bline in lines:
        processed_lines += 1
        if not bline.strip():
            continue

        h = sha1_hex(bline)
        if h in raw_hash_seen:
            dropped_raw += 1
            continue
        raw_hash_seen.add(h)

        try:
            obj = json.loads(bline.decode("utf-8", errors="replace"))
        except Exception:
            dropped_raw += 1
            continue
        if not isinstance(obj, dict):
            dropped_raw += 1
            continue

        processed_events += 1
        ev = normalize_event(obj, ingest_ts)

        ok = ev.get("order_key") or "unknown:0"
        ouid = ev.get("order_uid") or ""
        sym = ev.get("symbol") or "UNKNOWN"
        ev_ts = ev.get("ts_utc") or ingest_ts

        stt = orders.get(ok)
        if stt is None:
            stt = OrderState(
                order_key=ok,
                order_uid=str(ouid),
                symbol=str(sym),
                first_seen_utc=ev_ts,
                last_seen_utc=ev_ts,
            )
            orders[ok] = stt
        else:
            stt.last_seen_utc = iso_max(stt.last_seen_utc, ev_ts)

        # status snapshots
        if ev.get("status") is not None:
            stt.status_last = str(ev["status"])
        if ev.get("total_qty") is not None:
            stt.total_qty_last = float(ev["total_qty"])
        if ev.get("filled") is not None:
            stt.filled_last = float(ev["filled"])
        if ev.get("remaining") is not None:
            stt.remaining_last = float(ev["remaining"])

        # exec handling with dedup
        if ev.get("kind") == "EXECUTION":
            ek = str(ev.get("exec_key") or "")
            if ek:
                if recent_exec.has(ek):
                    dropped_exec += 1
                else:
                    recent_exec.add(ek)
                    stt.execs[ek] = {
                        "qty": ev.get("execQty"),
                        "price": ev.get("execPrice"),
                        "ts_utc": ev.get("ts_utc"),
                        "execId": ev.get("execId"),
                    }
                    if len(stt.execs) > 1000:
                        for k_drop in list(stt.execs.keys())[:200]:
                            stt.execs.pop(k_drop, None)

        # terminal signal detection
        reject_sig = is_reject_event(ev)
        fill_sig = is_fill_event(ev)
        cancel_sig = is_cancel_event(ev)

        terminal_type: Optional[str] = None
        reason: List[str] = []

        # Priority: REJECT > FILL > CANCEL (policy choice; consistent)
        if reject_sig:
            terminal_type = "REJECT"
            reason.append("reject_signal")
        elif fill_sig:
            terminal_type = "FILL"
            reason.append("fill_signal")
        elif cancel_sig:
            terminal_type = "CANCEL"
            reason.append("cancel_signal")

        if terminal_type is None:
            continue

        # HARD RULE: only one terminal per order_key, ever.
        if stt.terminal is not None and bool(stt.terminal.get("finalized", False)):
            existing = str(stt.terminal.get("type") or "UNKNOWN")
            if terminal_type != existing:
                stt.anomalies.append(f"late_terminal_signal:{terminal_type}_after_{existing}")
                dropped_terminal += 1
            continue

        # if we already emitted terminal for this order_key in previous runs, do not emit again
        if terminal_emitted.has(ok):
            dropped_terminal += 1
            # keep anomaly if we see another terminal signal later
            if stt.terminal is None:
                stt.anomalies.append(f"terminal_seen_but_emitted_before:{terminal_type}")
            continue

        cum_exec = stt.cum_qty_from_execs()

        # High-severity anomaly: reject after any fill/exec
        if terminal_type == "REJECT" and (cum_exec > 0.0 or (stt.filled_last or 0.0) > 0.0):
            stt.anomalies.append("reject_after_fill_or_exec")

        # Race marker (not fatal by itself)
        if terminal_type == "CANCEL":
            if fill_sig or (
                stt.filled_last is not None
                and stt.total_qty_last is not None
                and abs(float(stt.filled_last) - float(stt.total_qty_last)) < 1e-9
            ):
                stt.anomalies.append("cancel_after_fill_race")

        # Fill completeness warning (not fatal in v2; stage5 v2.2 may add enrichment window)
        if terminal_type == "FILL":
            tq = stt.total_qty_last
            if isinstance(tq, (int, float)) and tq and tq > 0:
                if abs(cum_exec - float(tq)) > 1e-9:
                    warns.append(f"exec_details_incomplete_for_fill:{ok}")

        stt.terminal = {
            "type": terminal_type,
            "ts_utc": ingest_ts,
            "finalized": True,
            "reason": reason,
        }

        terminal_emitted.add(ok)
        new_terminal_events.append(
            {
                "ts_utc": ingest_ts,
                "order_key": ok,
                "order_uid": stt.order_uid,
                "symbol": stt.symbol,
                "terminal_type": terminal_type,
                "total_qty": stt.total_qty_last,
                "filled_qty_status": stt.filled_last,
                "cum_qty_execs": cum_exec,
                "exec_count": len(stt.execs),
                "anomalies": list(stt.anomalies)[-10:],
                "reason": reason,
            }
        )

    # Write outputs
    term_path = out_dir / "terminal_events.jsonl"
    with open(term_path, "w", encoding="utf-8") as f:
        for ev in new_terminal_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    snap_path = out_dir / "order_snapshots.jsonl"
    with open(snap_path, "w", encoding="utf-8") as f:
        for ok, stt in orders.items():
            f.write(
                json.dumps(
                    {
                        "order_key": ok,
                        "order_uid": stt.order_uid,
                        "symbol": stt.symbol,
                        "first_seen_utc": stt.first_seen_utc,
                        "last_seen_utc": stt.last_seen_utc,
                        "status_last": stt.status_last,
                        "total_qty_last": stt.total_qty_last,
                        "filled_last": stt.filled_last,
                        "remaining_last": stt.remaining_last,
                        "cum_qty_execs": stt.cum_qty_from_execs(),
                        "exec_count": len(stt.execs),
                        "terminal": stt.terminal,
                        "anomalies": stt.anomalies[-10:],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    # status/exit_code decision
    status = "OK"
    exit_code = 0

    high = []
    for stt in orders.values():
        if "reject_after_fill_or_exec" in stt.anomalies:
            high.append(stt.order_key)

    if high:
        status = "FAIL"
        exit_code = 2
        anomalies_top.append(f"reject_after_fill_or_exec:{len(high)}")
    elif warns:
        status = "WARN"
        exit_code = 1

    summary = {
        "schema": "stage5_terminal_proof_pack_v2_summary",
        "ts_utc": ingest_ts,
        "status": status,
        "exit_code": exit_code,
        "input_jsonl": str(in_jsonl),
        "watermark_before": {
            "offset": int(wm.get("offset") or 0),
            "size": int(wm.get("size") or 0),
            "mtime_utc": wm.get("mtime_utc"),
        },
        "watermark_after": {"offset": int(new_off), "size": size, "mtime_utc": mtime_utc},
        "processed": {"lines": processed_lines, "events": processed_events},
        "dedup": {"dropped_raw": dropped_raw, "dropped_exec": dropped_exec, "dropped_terminal": dropped_terminal},
        "terminal_counts": {
            "new_terminal_events": len(new_terminal_events),
            "fills": sum(1 for x in new_terminal_events if x["terminal_type"] == "FILL"),
            "cancels": sum(1 for x in new_terminal_events if x["terminal_type"] == "CANCEL"),
            "rejects": sum(1 for x in new_terminal_events if x["terminal_type"] == "REJECT"),
        },
        "warns": warns,
        "anomalies": anomalies_top,
        "out_dir": str(out_dir),
    }

    save_json(out_dir / "proof_summary.json", summary)
    save_json(latest_path, summary)

    # Persist cursor (bounded orders)
    items = list(orders.items())
    items.sort(key=lambda kv: kv[1].last_seen_utc)
    items = items[-args.max_orders :]

    orders_out: Dict[str, Any] = {}
    for ok, stt in items:
        orders_out[ok] = {
            "order_uid": stt.order_uid,
            "symbol": stt.symbol,
            "first_seen_utc": stt.first_seen_utc,
            "last_seen_utc": stt.last_seen_utc,
            "status_last": stt.status_last,
            "total_qty_last": stt.total_qty_last,
            "filled_last": stt.filled_last,
            "remaining_last": stt.remaining_last,
            "execs": stt.execs,
            "terminal": stt.terminal,
            "anomalies": stt.anomalies[-50:],
        }

    cur_out = {
        "schema": "stage5_terminal_proof_cursor_v2",
        "last_run_utc": ingest_ts,
        "last_status": {"status": status, "exit_code": exit_code},
        "watermark": {"offset": int(new_off), "size": size, "mtime_utc": mtime_utc},
        "recent_exec_keys": recent_exec.as_list(),
        # order_key only (one terminal per order)
        "terminal_emitted_keys": terminal_emitted.as_list(),
        "orders": orders_out,
    }
    save_json(cursor_path, cur_out)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
